from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import multidetect.cli as cli_module
from multidetect.cli import main
from multidetect.domain import VehicleTelemetry
from multidetect.model_manifest import (
    create_candidate_model_manifest,
    write_candidate_model_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/missions/fire_suppression.demo.json"
REPLAY = ROOT / "examples/fire_mission_replay.jsonl"
PATROL_CONFIG = ROOT / "configs/missions/fire_patrol.demo.json"
PAYLOAD_INVENTORY = ROOT / "examples/payload_inventory.demo.json"
FAILED_PAYLOAD_INVENTORY = ROOT / "examples/payload_inventory.failed.demo.json"
EVALUATION_GROUND_TRUTH = ROOT / "examples/evaluation_ground_truth.demo.jsonl"
EVALUATION_PREDICTIONS = ROOT / "examples/evaluation_predictions.demo.jsonl"


def parsed_stdout(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_validate_config_command(capsys) -> None:
    assert main(["validate-config", str(CONFIG)]) == 0

    output = parsed_stdout(capsys)
    assert output[-1]["event"] == "config_valid"
    assert output[-1]["human_authorization_required"] is True


def test_replay_stops_at_redacted_authorization(capsys) -> None:
    assert main(["replay", str(CONFIG), str(REPLAY)]) == 0

    output = parsed_stdout(capsys)
    challenge = next(item for item in output if item["event"] == "authorization_required")
    finished = output[-1]
    assert challenge["nonce_redacted"] is True
    assert "nonce" not in challenge
    assert finished["pending_authorization"] is True
    assert finished["fake_release_request_count"] == 0


def test_explicit_simulation_cycle_writes_audit(tmp_path: Path, capsys) -> None:
    audit_path = tmp_path / "audit.jsonl"
    assert (
        main(
            [
                "replay",
                str(CONFIG),
                str(REPLAY),
                "--simulate-authorized-cycle",
                "--audit-out",
                str(audit_path),
            ]
        )
        == 0
    )

    output = parsed_stdout(capsys)
    finished = output[-1]
    audit_records = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert finished["simulated_cycle_completed"] is True
    assert finished["fake_release_request_count"] == 1
    assert any(record["event_type"] == "payload.release_confirmed" for record in audit_records)


def test_patrol_replay_reports_alert_without_authorization(capsys) -> None:
    assert main(["replay", str(PATROL_CONFIG), str(REPLAY)]) == 0

    output = parsed_stdout(capsys)
    alerts = [item for item in output if item["event"] == "fire_alert_confirmed"]
    finished = output[-1]
    assert len(alerts) == 1
    assert alerts[0]["delivery"] == "local_console_only"
    assert not any(item["event"] == "authorization_required" for item in output)
    assert finished["mission_capability"] == "patrol_only"
    assert finished["payload_installed"] is False
    assert finished["alert_count"] == 1
    assert finished["pending_authorization"] is False
    assert finished["fake_release_request_count"] == 0


def test_patrol_replay_rejects_authorized_payload_cycle(capsys) -> None:
    assert (
        main(
            [
                "replay",
                str(PATROL_CONFIG),
                str(REPLAY),
                "--simulate-authorized-cycle",
            ]
        )
        == 1
    )

    error = json.loads(capsys.readouterr().err)
    assert error["event"] == "error"
    assert "installed payload" in error["message"]


def test_camera_check_can_soak_multiple_frames(monkeypatch, capsys) -> None:
    class _Source:
        reconnect_count = 0

        def __init__(self, _config) -> None:
            self.count = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def read(self):
            self.count += 1
            return SimpleNamespace(
                width=640,
                height=480,
                frame_id=f"frame-{self.count}",
            )

    monkeypatch.setattr(cli_module, "OpenCVFrameSource", _Source)

    assert main(["camera-check", "--source", "0", "--frames", "3"]) == 0

    result = parsed_stdout(capsys)[-1]
    assert result["frame_count"] == 3
    assert result["frame_id"] == "frame-3"
    assert result["reconnect_count"] == 0
    assert result["average_fps"] > 0


def test_operator_link_demo_closes_selection_and_tracking_loop(capsys) -> None:
    assert main(["operator-link-demo"]) == 0

    output = parsed_stdout(capsys)
    encoded = next(item for item in output if item["event"] == "operator_selection_encoded")
    acknowledgement = next(item for item in output if item["event"] == "g20_selection_acknowledged")
    tracking = next(item for item in output if item["event"] == "g20_track_status_received")
    finished = output[-1]

    assert encoded["payload_bytes"] <= encoded["maximum_payload_bytes"] == 128
    assert acknowledgement["accepted"] is True
    assert acknowledgement["attempts"] == 3
    assert acknowledgement["jetson_detected_duplicate"] is True
    assert tracking["state"] == "tracking"
    assert tracking["payload_bytes"] <= 128
    assert finished["selection_delivered"] is True
    assert finished["tracking_status_received"] is True
    assert finished["physical_payload_interface_present"] is False
    assert finished["autopilot_write_enabled"] is False


def test_operator_udp_cli_rejects_bad_signing_key_without_echoing_secrets(
    monkeypatch, capsys
) -> None:
    operator_secret = "OPERATOR_SECRET_MUST_NOT_APPEAR_1234567890"
    signing_secret = "NOT_HEX_MAVLINK_SECRET"
    monkeypatch.setenv("TEST_OPERATOR_HMAC", operator_secret)
    monkeypatch.setenv("TEST_MAVLINK_HEX", signing_secret)

    assert (
        main(
            [
                "operator-udp-select",
                "--host",
                "127.0.0.1",
                "--operator-hmac-key-env",
                "TEST_OPERATOR_HMAC",
                "--mavlink-signing-key-hex-env",
                "TEST_MAVLINK_HEX",
            ]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert "64 hexadecimal" in captured.err
    assert operator_secret not in captured.out + captured.err
    assert signing_secret not in captured.out + captured.err


def test_camera_source_can_be_read_from_environment_without_echoing_secret(
    monkeypatch, capsys
) -> None:
    secret_uri = "rtsp://SECRET_USER:SECRET_PASSWORD@camera.invalid/stream"
    received = {}

    def fake_camera_check(config, *, frame_count):
        received["source"] = config.source
        received["frame_count"] = frame_count
        return 0

    monkeypatch.setenv("MULTIDETECT_TEST_CAMERA", secret_uri)
    monkeypatch.setattr(cli_module, "_camera_check", fake_camera_check)

    assert (
        main(
            [
                "camera-check",
                "--source-env",
                "MULTIDETECT_TEST_CAMERA",
                "--frames",
                "2",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert received == {"source": secret_uri, "frame_count": 2}
    assert "SECRET_USER" not in captured.out + captured.err
    assert "SECRET_PASSWORD" not in captured.out + captured.err


def test_camera_source_env_rejects_missing_variable_without_exposing_values(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("MISSING_CAMERA_SOURCE", raising=False)

    assert main(["camera-check", "--source-env", "MISSING_CAMERA_SOURCE"]) == 1

    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "ValueError"
    assert "MISSING_CAMERA_SOURCE" in error["message"]


def test_model_check_reports_contract_hash_and_latency(tmp_path, monkeypatch, capsys) -> None:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"deterministic-test-model")

    class _Detector:
        class_names = ("fire", "smoke")
        provider_names = ("CPUExecutionProvider",)

        received_config = None

        def __init__(self, config) -> None:
            type(self).received_config = config

        def detect(self, _image) -> tuple:
            return ()

    monkeypatch.setattr(cli_module, "OnnxNx6Detector", _Detector)

    assert (
        main(
            [
                "model-check",
                "--onnx-model",
                str(model),
                "--warmup-iterations",
                "1",
                "--benchmark-iterations",
                "2",
                "--output-coordinates",
                "normalized_xyxy",
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    assert result["event"] == "onnx_model_validated"
    assert len(result["model_sha256"]) == 64
    assert result["post_nms_output_contract"] == "Nx6"
    assert result["active_providers"] == ["CPUExecutionProvider"]
    assert result["accuracy_validated"] is False
    assert _Detector.received_config.output_coordinates == "normalized_xyxy"


def test_model_check_production_gate_requires_manifest(tmp_path, capsys) -> None:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"model")

    assert (
        main(
            [
                "model-check",
                "--onnx-model",
                str(model),
                "--require-production-approved",
            ]
        )
        == 1
    )

    error = json.loads(capsys.readouterr().err)
    assert "model manifest is required" in error["message"]


def test_model_manifest_init_creates_quarantined_hash_bound_manifest(
    tmp_path: Path,
    capsys,
) -> None:
    model = tmp_path / "fire.onnx"
    manifest = tmp_path / "fire.manifest.json"
    model.write_bytes(b"candidate-model")

    assert (
        main(
            [
                "model-manifest-init",
                "--onnx-model",
                str(model),
                "--out",
                str(manifest),
                "--model-id",
                "fire-candidate",
                "--model-version",
                "candidate-v1",
                "--source-description",
                "isolated export supplied by operator",
                "--output-coordinates",
                "normalized_xyxy",
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert result["event"] == "candidate_model_manifest_created"
    assert document["status"] == "quarantined"
    assert document["model_role"] == "fire_candidate"
    assert document["governance"]["production_approved"] is False
    assert document["export"]["artifact_sha256"] == result["model_sha256"]


def test_legacy_checkpoint_verify_only_checks_bytes(tmp_path: Path, capsys) -> None:
    checkpoint = tmp_path / "best.pt"
    content = b"opaque-checkpoint"
    checkpoint.write_bytes(content)

    assert (
        main(
            [
                "legacy-checkpoint-verify",
                str(checkpoint),
                "--expected-size-bytes",
                str(len(content)),
                "--expected-sha256",
                hashlib.sha256(content).hexdigest(),
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    assert result["matches_audited_checkpoint"] is True
    assert result["deserialized"] is False
    assert result["safe_to_run_directly"] is False
    assert result["requires_isolated_export"] is True


def test_pixhawk_check_reports_read_only_telemetry(monkeypatch, capsys) -> None:
    class _Provider:
        is_read_only = True

        def __init__(self, config) -> None:
            self.config = config
            self.closed = False

        def snapshot(self, *, now_s: float) -> VehicleTelemetry:
            assert now_s >= 0
            return VehicleTelemetry(
                altitude_agl_m=42.5,
                roll_deg=1.2,
                pitch_deg=-0.8,
                ground_speed_mps=17.0,
                in_allowed_zone=None,
                geofence_healthy=None,
                position_healthy=True,
                link_healthy=True,
                flight_mode_allows_deploy=None,
                release_zone_clear=None,
                latitude_deg=31.123456,
                longitude_deg=121.654321,
                heading_deg=90.0,
                battery_remaining_pct=81.0,
                satellites_visible=18,
                armed=True,
                flight_mode="AUTO",
                mission_sequence=3,
            )

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(cli_module, "PixhawkReadOnlyTelemetryProvider", _Provider)

    assert (
        main(
            [
                "pixhawk-check",
                "--endpoint",
                "udp:127.0.0.1:14550",
                "--samples",
                "3",
                "--interval-seconds",
                "0",
                "--require-fresh-link",
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    assert result["event"] == "pixhawk_read_only_check_finished"
    assert result["fresh_link_sample_count"] == 3
    assert result["latest"]["flight_mode"] == "AUTO"
    assert result["messages_transmitted"] == 0
    assert result["hardware_control_enabled"] is False


def test_live_patrol_rejects_payload_cycle_flag(monkeypatch, capsys) -> None:
    class _Detector:
        provider_names = ("CPUExecutionProvider",)
        class_names = ("fire", "smoke")

        def __init__(self, _config) -> None:
            pass

    monkeypatch.setattr(cli_module, "OnnxNx6Detector", _Detector)

    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--simulate-payload-cycle",
            ]
        )
        == 1
    )

    error = json.loads(capsys.readouterr().err)
    assert "installed payload" in error["message"]


def test_live_observed_lifecycle_requires_pixhawk_endpoint(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--observe-pixhawk-lifecycle",
                "--task-area-mission-sequence",
                "2",
            ]
        )
        == 1
    )

    error = json.loads(capsys.readouterr().err)
    assert "requires --pixhawk-endpoint" in error["message"]


def test_live_rejects_fire_manifest_used_as_safety_object_model(
    tmp_path, monkeypatch, capsys
) -> None:
    model = tmp_path / "candidate.onnx"
    model.write_bytes(b"role-gate-placeholder")
    manifest = write_candidate_model_manifest(
        tmp_path / "fire.manifest.json",
        create_candidate_model_manifest(
            model,
            model_id="fire-candidate",
            model_version="v1",
            class_names=("fire", "smoke"),
            input_width=640,
            input_height=640,
            output_coordinates="normalized_xyxy",
            source_description="test fire candidate",
            model_role="fire_candidate",
        ),
    )

    class _Detector:
        provider_names = ("CPUExecutionProvider",)
        class_names = ("fire", "smoke")

        def __init__(self, _config) -> None:
            pass

    monkeypatch.setattr(cli_module, "OnnxNx6Detector", _Detector)

    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                str(model),
                "--model-manifest",
                str(manifest),
                "--output-coordinates",
                "normalized_xyxy",
                "--safety-onnx-model",
                str(model),
                "--safety-model-manifest",
                str(manifest),
                "--safety-output-coordinates",
                "normalized_xyxy",
            ]
        )
        == 1
    )

    error = json.loads(capsys.readouterr().err)
    assert "model role 'fire_candidate'" in error["message"]
    assert "safety_object_evidence" in error["message"]


def test_payload_inventory_check_accepts_matching_hil_report(capsys) -> None:
    assert (
        main(
            [
                "payload-inventory-check",
                str(CONFIG),
                str(PAYLOAD_INVENTORY),
                "--now-s",
                "1000.5",
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    assert result["event"] == "payload_inventory_checked"
    assert result["allowed"] is True
    assert result["hardware_control_enabled"] is False


def test_payload_inventory_check_rejects_failed_interlock_and_slot(capsys) -> None:
    assert (
        main(
            [
                "payload-inventory-check",
                str(CONFIG),
                str(FAILED_PAYLOAD_INVENTORY),
                "--now-s",
                "1001.5",
            ]
        )
        == 1
    )

    result = parsed_stdout(capsys)[-1]
    assert result["allowed"] is False
    assert any("interlock" in reason for reason in result["reasons"])
    assert any("not locked" in reason for reason in result["reasons"])


def test_evaluate_detections_command_reports_metrics(capsys) -> None:
    assert (
        main(
            [
                "evaluate-detections",
                str(EVALUATION_GROUND_TRUTH),
                str(EVALUATION_PREDICTIONS),
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    assert result["event"] == "detection_evaluation_completed"
    assert result["overall"]["precision"] == pytest.approx(0.5)
    assert result["overall"]["recall"] == pytest.approx(0.5)

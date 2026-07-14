from __future__ import annotations

import hashlib
import json
import struct
import threading
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
from multidetect.pixhawk_parameters import (
    PixhawkParameterRecord,
    PixhawkParameterSnapshot,
    write_pixhawk_parameter_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/missions/fire_suppression.demo.json"
REPLAY = ROOT / "examples/fire_mission_replay.jsonl"
PATROL_CONFIG = ROOT / "configs/missions/fire_patrol.demo.json"
FIXED_WING_CONFIG = ROOT / "configs/missions/fire_suppression_fixed_wing.demo.json"
FIXED_WING_REPLAY = ROOT / "examples/fire_fixed_wing_hil_replay.jsonl"
PAYLOAD_INVENTORY = ROOT / "examples/payload_inventory.demo.json"
FAILED_PAYLOAD_INVENTORY = ROOT / "examples/payload_inventory.failed.demo.json"
EVALUATION_GROUND_TRUTH = ROOT / "examples/evaluation_ground_truth.demo.jsonl"
EVALUATION_PREDICTIONS = ROOT / "examples/evaluation_predictions.demo.jsonl"


def parsed_stdout(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_operator_udp_listeners_default_to_loopback() -> None:
    parser = cli_module.build_parser()

    server = parser.parse_args(
        [
            "operator-udp-server",
            "--operator-hmac-key-env",
            "OPERATOR_KEY",
            "--mavlink-signing-key-hex-env",
            "MAVLINK_KEY",
        ]
    )
    live = parser.parse_args(
        [
            "live-camera",
            str(CONFIG),
            "--onnx-model",
            "model.onnx",
            "--class-names",
            "fire,smoke",
        ]
    )

    assert server.bind_host == "127.0.0.1"
    assert live.operator_udp_bind_host == "127.0.0.1"


def test_validate_config_command(capsys) -> None:
    assert main(["validate-config", str(CONFIG)]) == 0

    output = parsed_stdout(capsys)
    assert output[-1]["event"] == "config_valid"
    assert output[-1]["human_authorization_required"] is True


def test_release_window_check_is_advisory_only(capsys) -> None:
    assert (
        main(
            [
                "release-window-check",
                str(FIXED_WING_CONFIG),
                "--x1",
                "0.45",
                "--y1",
                "0.40",
                "--x2",
                "0.55",
                "--y2",
                "0.50",
                "--altitude-agl-m",
                "40",
                "--ground-speed-mps",
                "18",
                "--pitch-deg",
                "0",
                "--now-s",
                "100",
            ]
        )
        == 0
    )

    output = parsed_stdout(capsys)[-1]
    assert output["event"] == "fixed_wing_release_window_checked"
    assert output["status"] == "ready"
    assert output["advisory_only"] is True
    assert output["safety_rules_evaluated"] is False
    assert output["authorization_created"] is False
    assert output["flight_control_enabled"] is False
    assert output["physical_release_enabled"] is False


def test_release_window_check_rejects_patrol_config(capsys) -> None:
    result = main(
        [
            "release-window-check",
            str(PATROL_CONFIG),
            "--x1",
            "0.45",
            "--y1",
            "0.40",
            "--x2",
            "0.55",
            "--y2",
            "0.50",
            "--altitude-agl-m",
            "40",
            "--ground-speed-mps",
            "18",
            "--pitch-deg",
            "0",
        ]
    )

    assert result == 1
    error = json.loads(capsys.readouterr().err)
    assert "does not configure" in error["message"]


def test_fixed_wing_hil_replay_requires_authorization_and_confirms_fake_cycle(
    capsys,
) -> None:
    assert (
        main(
            [
                "replay",
                str(FIXED_WING_CONFIG),
                str(FIXED_WING_REPLAY),
                "--simulate-authorized-cycle",
                "--operator-id",
                "fixed-wing-test-operator",
            ]
        )
        == 0
    )

    output = parsed_stdout(capsys)
    authorized_frame = next(
        item for item in output if item["event"] == "frame_evaluated" and item["decisions"]
    )
    challenge = next(item for item in output if item["event"] == "authorization_required")
    finished = output[-1]
    window = authorized_frame["decisions"][0]["deployment_window"]
    assert window["status"] == "ready"
    assert window["advisory_only"] is True
    assert window["flight_control_enabled"] is False
    assert window["physical_release_enabled"] is False
    assert challenge["nonce_redacted"] is True
    assert finished["simulated_cycle_completed"] is True
    assert finished["fake_release_request_count"] == 1
    assert finished["phase"] == "return_requested"


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
    mission = next(item for item in output if item["event"] == "g20_mission_status_received")
    safety = next(item for item in output if item["event"] == "g20_safety_status_received")
    finished = output[-1]

    assert encoded["payload_bytes"] <= encoded["maximum_payload_bytes"] == 128
    assert acknowledgement["accepted"] is True
    assert acknowledgement["attempts"] == 3
    assert acknowledgement["jetson_detected_duplicate"] is True
    assert tracking["state"] == "tracking"
    assert tracking["payload_bytes"] <= 128
    assert mission["payload_bytes"] == 89
    assert mission["authorization_state"] == "pending"
    assert mission["release_window"] == "wait"
    assert mission["advisory_only"] is True
    assert mission["flight_control_enabled"] is False
    assert mission["physical_release_enabled"] is False
    assert safety["payload_bytes"] == 86
    assert safety["pass_count"] == 1
    assert safety["deny_count"] == 1
    assert safety["unknown_count"] == 1
    assert safety["allowed"] is False
    assert safety["physical_release_enabled"] is False
    assert finished["selection_delivered"] is True
    assert finished["tracking_status_received"] is True
    assert finished["mission_status_received"] is True
    assert finished["safety_status_received"] is True
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


def test_operator_udp_authorization_cli_closes_signed_protocol_hil_loop(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_OPERATOR_HMAC", "A" * 32)
    monkeypatch.setenv("TEST_MAVLINK_HEX", "42" * 32)
    events: list[dict[str, object]] = []
    ready = threading.Event()

    def record(document, *, stream=None) -> None:
        del stream
        events.append(dict(document))
        if document.get("event") == "operator_udp_server_ready":
            ready.set()

    monkeypatch.setattr(cli_module, "_emit", record)
    server_result: list[int] = []
    server = threading.Thread(
        target=lambda: server_result.append(
            main(
                [
                    "operator-udp-server",
                    "--port",
                    "0",
                    "--operator-hmac-key-env",
                    "TEST_OPERATOR_HMAC",
                    "--mavlink-signing-key-hex-env",
                    "TEST_MAVLINK_HEX",
                    "--authorization-hil",
                    "--max-datagrams",
                    "2",
                    "--receive-timeout-seconds",
                    "5",
                ]
            )
        ),
        daemon=True,
    )
    server.start()
    assert ready.wait(timeout=2.0)
    ready_event = next(item for item in events if item["event"] == "operator_udp_server_ready")

    client_result = main(
        [
            "operator-udp-authorize",
            "--host",
            "127.0.0.1",
            "--port",
            str(ready_event["port"]),
            "--operator-hmac-key-env",
            "TEST_OPERATOR_HMAC",
            "--mavlink-signing-key-hex-env",
            "TEST_MAVLINK_HEX",
            "--operator-id",
            "g20-test-operator",
            "--decision",
            "approve",
        ]
    )
    server.join(timeout=5.0)

    assert client_result == 0
    assert server.is_alive() is False
    assert server_result == [0]
    challenge = next(
        item for item in events if item["event"] == "operator_udp_authorization_challenge_published"
    )
    processed = next(
        item for item in events if item["event"] == "operator_udp_authorization_decision_processed"
    )
    acknowledged = next(
        item
        for item in events
        if item["event"] == "operator_udp_authorization_decision_acknowledged"
    )
    assert challenge["nonce_transmitted"] is False
    assert processed["accepted"] is True
    assert processed["decision"] == "approve"
    assert processed["mission_state_changed"] is False
    assert processed["payload_release_requested"] is False
    assert acknowledged["accepted"] is True
    assert acknowledged["flight_command_enabled"] is False
    assert acknowledged["hardware_control_enabled"] is False


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
        messages_transmitted = 0

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
    assert result["fresh_transport_link_sample_count"] == 3
    assert result["latest"]["flight_mode"] == "AUTO"
    assert result["gate_passed"] is True
    assert result["messages_transmitted"] == 0
    assert result["hardware_control_enabled"] is False


def test_pixhawk_check_reports_identity_gate_failures(monkeypatch, capsys) -> None:
    class _Document:
        def __init__(self, document: dict[str, object]) -> None:
            self.document = document

        def to_document(self) -> dict[str, object]:
            return self.document

    class _Provider:
        is_read_only = True
        messages_transmitted = 0
        messages_received = 20
        rejected_system_messages = 0
        ignored_non_autopilot_heartbeats = 0
        message_type_counts = {"HEARTBEAT": 2, "ATTITUDE": 18}
        heartbeat_identity = _Document(
            {
                "source_system_id": 1,
                "autopilot_id": 12,
                "autopilot_name": "MAV_AUTOPILOT_PX4",
                "vehicle_type_id": 0,
                "vehicle_type_name": "MAV_TYPE_GENERIC",
                "system_status_id": 0,
                "system_status_name": "MAV_STATE_UNINIT",
            }
        )
        qualification = _Document(
            {
                "required": True,
                "passed": False,
                "reasons": (
                    "vehicle type mismatch: expected=1, actual=0",
                    "system status is not operational: MAV_STATE_UNINIT",
                ),
            }
        )

        def __init__(self, config) -> None:
            self.config = config

        def snapshot(self, *, now_s: float) -> VehicleTelemetry:
            return VehicleTelemetry(
                altitude_agl_m=float("nan"),
                roll_deg=0.0,
                pitch_deg=0.0,
                ground_speed_mps=float("nan"),
                in_allowed_zone=None,
                geofence_healthy=None,
                position_healthy=None,
                link_healthy=False,
                flight_mode_allows_deploy=None,
                release_zone_clear=None,
                armed=False,
                flight_mode="LOITER",
            )

        def transport_link_healthy(self, *, now_s: float) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "PixhawkReadOnlyTelemetryProvider", _Provider)

    assert (
        main(
            [
                "pixhawk-check",
                "--endpoint",
                "udp:0.0.0.0:14550",
                "--samples",
                "2",
                "--interval-seconds",
                "0",
                "--require-fresh-link",
                "--require-fresh-position",
                "--expected-system-id",
                "1",
                "--expected-autopilot",
                "px4",
                "--expected-vehicle-type",
                "fixed_wing",
                "--require-operational-state",
            ]
        )
        == 1
    )

    result = parsed_stdout(capsys)[-1]
    assert result["fresh_transport_link_sample_count"] == 2
    assert result["fresh_link_sample_count"] == 0
    assert result["fresh_position_sample_count"] == 0
    assert result["gate_passed"] is False
    assert "no fresh global position was received" in result["gate_failures"]
    assert any("vehicle type mismatch" in item for item in result["gate_failures"])
    assert result["heartbeat_identity"]["autopilot_name"] == "MAV_AUTOPILOT_PX4"


def test_pixhawk_parameter_backup_requires_explicit_active_read_acknowledgement(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    constructed = []

    class _Client:
        def __init__(self, config) -> None:
            constructed.append(config)

    monkeypatch.setattr(cli_module, "PixhawkParameterBackupClient", _Client)

    assert (
        main(
            [
                "pixhawk-param-backup",
                "--endpoint",
                "udpout:127.0.0.1:14550",
                "--target-system-id",
                "1",
                "--parameter-encoding",
                "bytewise",
                "--out",
                str(tmp_path / "parameters.json"),
            ]
        )
        == 1
    )

    error = json.loads(capsys.readouterr().err)
    assert "--acknowledge-active-read-request" in error["message"]
    assert "no MAVLink request was sent" in error["message"]
    assert constructed == []


def test_pixhawk_parameter_backup_writes_explicit_read_only_snapshot(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    clients = []

    class _Snapshot:
        passed = True

        def to_document(self) -> dict[str, object]:
            return {
                "event": "pixhawk_parameter_backup_completed",
                "complete": True,
                "passed": True,
                "messages_transmitted": 1,
                "parameter_write_messages_transmitted": 0,
                "flight_command_messages_transmitted": 0,
                "parameters": [],
            }

    class _Client:
        def __init__(self, config) -> None:
            self.config = config
            self.closed = False
            clients.append(self)

        def capture(self):
            return _Snapshot()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(cli_module, "PixhawkParameterBackupClient", _Client)
    output = tmp_path / "parameters.json"

    assert (
        main(
            [
                "pixhawk-param-backup",
                "--endpoint",
                "udpout:127.0.0.1:14550",
                "--target-system-id",
                "1",
                "--parameter-encoding",
                "bytewise",
                "--minimum-parameters",
                "1",
                "--out",
                str(output),
                "--acknowledge-active-read-request",
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert clients[0].closed is True
    assert clients[0].config.target_system_id == 1
    assert result["messages_transmitted"] == 1
    assert result["parameter_write_messages_transmitted"] == 0
    assert persisted["flight_command_messages_transmitted"] == 0


def test_pixhawk_parameter_verify_and_diff_are_offline_and_fail_closed(
    tmp_path: Path,
    capsys,
) -> None:
    def write_snapshot(path: Path, value: float) -> None:
        raw_value_hex = struct.pack("<f", value).hex()
        write_pixhawk_parameter_snapshot(
            path,
            PixhawkParameterSnapshot(
                captured_at_utc="2026-07-13T00:00:00+00:00",
                configured_endpoint="udpout:127.0.0.1:14550",
                resolved_endpoint="udpout:127.0.0.1:14550",
                parameter_encoding="c_cast",
                target_system_id=1,
                target_component_id=1,
                duration_seconds=0.1,
                expected_parameter_count=1,
                received_parameter_count=1,
                rejected_source_message_count=0,
                invalid_parameter_message_count=0,
                active_read_requests_transmitted=1,
                px4_parameter_hash_raw_hex=None,
                parameters=(
                    PixhawkParameterRecord(
                        name="PARAM_A",
                        value=value,
                        raw_value_hex=raw_value_hex,
                        parameter_type=9,
                        index=0,
                    ),
                ),
                complete=True,
                passed=True,
                failure_reasons=(),
            ),
        )

    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    verify_report = tmp_path / "verify.json"
    rejected_diff = tmp_path / "rejected-diff.json"
    accepted_diff = tmp_path / "accepted-diff.json"
    write_snapshot(before, 1.0)
    write_snapshot(after, 2.0)

    assert (
        main(
            [
                "pixhawk-param-verify",
                str(before),
                "--out",
                str(verify_report),
            ]
        )
        == 0
    )
    verified = parsed_stdout(capsys)[-1]
    assert verified["self_consistency_hash_verified"] is True
    assert verified["cryptographically_authenticated"] is False
    assert verified["messages_transmitted"] == 0

    assert (
        main(
            [
                "pixhawk-param-diff",
                str(before),
                str(after),
                "--out",
                str(rejected_diff),
            ]
        )
        == 1
    )
    rejected = parsed_stdout(capsys)[-1]
    assert rejected["gate_passed"] is False
    assert rejected["unexpected_change_names"] == ["PARAM_A"]
    assert rejected["messages_transmitted"] == 0

    assert (
        main(
            [
                "pixhawk-param-diff",
                str(before),
                str(after),
                "--allow-change",
                "PARAM_A",
                "--require-change",
                "PARAM_A",
                "--out",
                str(accepted_diff),
            ]
        )
        == 0
    )
    accepted = parsed_stdout(capsys)[-1]
    persisted = json.loads(accepted_diff.read_text(encoding="utf-8"))
    assert accepted["gate_passed"] is True
    assert persisted["observed_change_names"] == ["PARAM_A"]
    assert persisted["hardware_control_enabled"] is False


def test_pixhawk_link_audit_is_offline_and_separates_transport_roles(
    tmp_path: Path,
    capsys,
) -> None:
    values = {
        "MAV_0_CONFIG": 101,
        "MAV_0_MODE": 0,
        "MAV_0_RATE": 1200,
        "MAV_0_FORWARD": 1,
        "MAV_1_CONFIG": 0,
        "MAV_2_CONFIG": 1000,
        "MAV_2_MODE": 0,
        "MAV_2_RATE": 100000,
        "MAV_2_FORWARD": 0,
        "MAV_2_BROADCAST": 1,
        "MAV_2_REMOTE_PRT": 14550,
        "MAV_2_UDP_PRT": 14550,
        "SER_TEL1_BAUD": 115200,
    }
    parameters = tuple(
        PixhawkParameterRecord(
            name=name,
            value=value,
            raw_value_hex=struct.pack("<i", value).hex(),
            parameter_type=6,
            index=index,
        )
        for index, (name, value) in enumerate(values.items())
    )
    snapshot = tmp_path / "parameters.json"
    report = tmp_path / "link-audit.json"
    write_pixhawk_parameter_snapshot(
        snapshot,
        PixhawkParameterSnapshot(
            captured_at_utc="2026-07-14T00:00:00+00:00",
            configured_endpoint="tcp:192.168.144.11:5760",
            resolved_endpoint="tcp:192.168.144.11:5760",
            parameter_encoding="bytewise",
            target_system_id=1,
            target_component_id=1,
            duration_seconds=1.0,
            expected_parameter_count=len(parameters),
            received_parameter_count=len(parameters),
            rejected_source_message_count=0,
            invalid_parameter_message_count=0,
            active_read_requests_transmitted=1,
            px4_parameter_hash_raw_hex=None,
            parameters=parameters,
            complete=True,
            passed=True,
            failure_reasons=(),
        ),
    )

    assert (
        main(
            [
                "pixhawk-link-audit",
                str(snapshot),
                "--out",
                str(report),
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    persisted = json.loads(report.read_text(encoding="utf-8"))
    assert result["event"] == "pixhawk_v6x_link_topology_audited"
    assert result["links"]["gr01_v6x_telem1"]["configured_baud"] == 115200
    assert result["links"]["jetson_v6x_primary"]["baud_applies"] is False
    assert result["uart_fallback_ready"] is False
    assert result["gate_passed"] is True
    assert persisted["messages_transmitted"] == 0
    assert persisted["parameter_write_messages_transmitted"] == 0
    assert persisted["hardware_contacted"] is False

    assert (
        main(
            [
                "pixhawk-link-audit",
                str(snapshot),
                "--require-uart-fallback",
            ]
        )
        == 1
    )
    required = parsed_stdout(capsys)[-1]
    assert required["primary_configuration_passed"] is True
    assert required["gate_passed"] is False


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


def test_live_tensorrt_engine_requires_hash_bound_manifest(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "fire.engine",
            ]
        )
        == 1
    )

    error = json.loads(capsys.readouterr().err)
    assert "hash-bound --model-manifest" in error["message"]


def test_live_auto_payload_hil_requires_explicit_simulation_flag(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--auto-simulate-payload-cycle",
            ]
        )
        == 1
    )

    error = json.loads(capsys.readouterr().err)
    assert "requires --simulate-payload-cycle" in error["message"]


def test_live_inert_payload_hil_requires_explicit_complete_configuration(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--payload-hil-controller-port",
                "15001",
            ]
        )
        == 1
    )
    assert "require --inert-payload-hil" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                "live-camera",
                str(CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--inert-payload-hil",
            ]
        )
        == 1
    )
    assert "requires --simulate-payload-cycle" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                "live-camera",
                str(CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--simulate-payload-cycle",
                "--inert-payload-hil",
            ]
        )
        == 1
    )
    assert (
        "requires --payload-hil-controller-port" in json.loads(capsys.readouterr().err)["message"]
    )


def test_live_inert_payload_hil_keys_are_separate_and_environment_only(monkeypatch, capsys) -> None:
    monkeypatch.setenv("HIL_REQUEST_KEY", "same-key-material-that-is-at-least-32-bytes")
    monkeypatch.setenv("HIL_RESULT_KEY", "same-key-material-that-is-at-least-32-bytes")
    monkeypatch.setenv("HIL_CONFIRM_KEY", "another-confirmation-key-at-least-32-bytes")
    arguments = [
        "live-camera",
        str(CONFIG),
        "--onnx-model",
        "unused.onnx",
        "--simulate-payload-cycle",
        "--inert-payload-hil",
        "--payload-hil-controller-port",
        "15001",
        "--payload-hil-controller-module-id",
        "controller-1",
        "--payload-hil-request-key-env",
        "HIL_REQUEST_KEY",
        "--payload-hil-request-key-id",
        "request-v1",
        "--payload-hil-result-key-env",
        "HIL_RESULT_KEY",
        "--payload-hil-result-key-id",
        "result-v1",
        "--payload-confirmation-port",
        "15002",
        "--payload-confirmation-key-env",
        "HIL_CONFIRM_KEY",
        "--payload-confirmation-key-id",
        "confirm-v1",
        "--payload-confirmation-sensor-id",
        "bay-sensor-1",
    ]

    assert main(arguments) == 1
    assert "keys must differ" in json.loads(capsys.readouterr().err)["message"]

    monkeypatch.setenv("HIL_RESULT_KEY", "unique-result-key-material-at-least-32-bytes")
    arguments[-1] = "controller-1"
    assert main(arguments) == 1
    assert "IDs must differ" in json.loads(capsys.readouterr().err)["message"]


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


def test_live_observed_lifecycle_requires_flight_controller_identity_gate(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--pixhawk-endpoint",
                "udp:0.0.0.0:14550",
                "--observe-pixhawk-lifecycle",
                "--task-area-mission-sequence",
                "2",
            ]
        )
        == 1
    )

    error = json.loads(capsys.readouterr().err)
    assert "requires --pixhawk-system-id" in error["message"]
    assert "--pixhawk-expected-autopilot" in error["message"]
    assert "--pixhawk-expected-vehicle-type" in error["message"]
    assert "--require-pixhawk-operational-state" in error["message"]


def test_live_zone_evidence_requires_report_pixhawk_and_authentication(monkeypatch, capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--zone-evidence-hmac-key-env",
                "ZONE_KEY",
            ]
        )
        == 1
    )
    assert "require a report path" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--zone-evidence-report",
                "zone.json",
            ]
        )
        == 1
    )
    assert "requires --pixhawk-endpoint" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--zone-evidence-report",
                "zone.json",
                "--pixhawk-endpoint",
                "udp:127.0.0.1:14550",
            ]
        )
        == 1
    )
    assert "requires --zone-evidence-key-id" in json.loads(capsys.readouterr().err)["message"]

    monkeypatch.setenv("ZONE_KEY", "short")
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--zone-evidence-report",
                "zone.json",
                "--zone-evidence-key-id",
                "zone-key-v1",
                "--zone-evidence-hmac-key-env",
                "ZONE_KEY",
                "--pixhawk-endpoint",
                "udp:127.0.0.1:14550",
            ]
        )
        == 1
    )
    assert "at least 32 bytes" in json.loads(capsys.readouterr().err)["message"]


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
    constructed = []

    class _Detector:
        provider_names = ("CPUExecutionProvider",)
        class_names = ("fire", "smoke")

        def __init__(self, _config) -> None:
            constructed.append(_config)

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
    assert constructed == []


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

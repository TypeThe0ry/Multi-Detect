from __future__ import annotations

import hashlib
import json
import struct
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import multidetect.cli as cli_module
import multidetect.tracking_review as tracking_review_module
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
from multidetect.rtsp_evidence_recording import RtspEvidenceRecordingReport
from multidetect.video_evidence import VideoEvidenceProbe

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
TRACKING_GROUND_TRUTH = ROOT / "examples/tracking_identity_ground_truth.demo.jsonl"
TRACKING_PREDICTIONS = ROOT / "examples/tracking_identity_predictions.demo.jsonl"


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
    assert live.monocular_avoidance is False
    assert live.avoidance_analysis_width == 640
    assert live.unified_target_pool is False
    assert live.unified_target_pool_maximum_tracks == 64
    assert live.unified_target_pool_person_maximum_appearance_distance is None
    assert live.unified_target_pool_person_strict_reid_distance is None
    assert live.patrol_advisory is False
    assert live.patrol_maximum_bank_angle_deg == 25.0
    assert live.safety_priority_confidence_threshold == 0.25
    assert live.safety_fallback_confidence_threshold == 0.35
    assert live.fire_minimum_bright_warm_fraction == 0.0
    assert live.primary_model_frame_stride == 1
    assert live.primary_model_frame_phase == 0
    assert live.lock_model_force_every_frame is True
    assert live.safety_model_frame_stride == 1
    assert live.safety_model_frame_phase == 0
    assert live.safety_tile_columns == 1
    assert live.safety_tile_rows == 1
    assert live.safety_tile_fusion_iou_threshold == 0.30
    assert live.safety_tile_confidence_threshold == 0.40
    assert live.safety_tile_label_confidence_thresholds == "airplane=0.82"
    assert live.safety_tile_maximum_box_area == 0.04
    assert "person" in live.safety_tile_labels
    assert "airplane" in live.safety_tile_labels
    assert "car" in live.safety_tile_labels
    assert live.priority_onnx_model is None
    assert live.priority_input_width == 960
    assert live.priority_input_height == 960
    assert live.priority_confidence_threshold == 0.30
    assert live.priority_person_confidence_threshold == 0.30
    assert live.priority_vehicle_confidence_threshold == 0.60
    assert live.priority_label_confidence_thresholds == "truck=0.80"
    assert live.priority_vehicle_stability_frames == 3
    assert live.priority_model_frame_stride == 1
    assert live.priority_model_frame_phase == 0
    assert "pedestrian=person" in live.priority_label_map
    assert live.person_reid_onnx is None
    assert live.person_reid_engine is None
    assert live.vehicle_reid_onnx is None
    assert live.vehicle_reid_engine is None
    assert live.vehicle_reid_maximum_batch_size == 8
    assert live.person_reid_frame_stride == 2
    assert live.vehicle_reid_frame_stride == 2
    assert live.reid_maximum_interval_seconds == 0.1
    assert live.allow_nonrealtime_reid is False
    assert live.short_term_tracking is False
    assert live.short_term_analysis_width == 640
    assert live.short_term_minimum_box_size_px == 12
    assert live.short_term_frame_stride == 1
    assert live.short_term_search_expansion == 2.5
    assert live.short_term_occluded_search_multiplier == 1.5
    assert live.short_term_reacquiring_search_multiplier == 2.0
    assert live.short_term_maximum_search_expansion == 6.0
    assert live.short_term_maximum_retained_template_age_seconds == 2.0
    assert live.multimodal_ranging is False
    assert live.ranging_calibration is None
    assert live.environment_onnx_model is None
    assert live.environment_model_manifest is None
    assert live.environment_class_names == "power_line,flammable_tank"
    assert live.environment_confidence_threshold == 0.40
    assert live.semantic_context_onnx_model is None
    assert live.semantic_context_model_manifest is None
    assert live.semantic_context_engine is None
    assert live.semantic_context_engine_provenance is None
    assert live.semantic_context_minimum_interval_seconds == 0.5
    assert live.semantic_context_maximum_age_seconds == 2.0
    assert live.rgb_fire_verifier_model is None
    assert live.rgb_fire_verifier_model_manifest is None
    assert live.rgb_fire_verifier_confidence_threshold == 0.65
    assert live.rgb_fire_verifier_minimum_iou == 0.30
    assert live.identity_tracking_log_out is None
    assert live.identity_tracking_session_id is None


def test_live_independent_rgb_fire_verifier_cli_is_explicit() -> None:
    parser = cli_module.build_parser()

    live = parser.parse_args(
        [
            "live-camera",
            str(CONFIG),
            "--onnx-model",
            "primary.onnx",
            "--model-manifest",
            "primary.json",
            "--rgb-fire-verifier-model",
            "verifier.engine",
            "--rgb-fire-verifier-model-manifest",
            "verifier.json",
            "--rgb-fire-verifier-class-names",
            "fire,smoke",
            "--rgb-fire-verifier-confidence-threshold",
            "0.70",
            "--rgb-fire-verifier-minimum-iou",
            "0.40",
            "--rgb-fire-verifier-output-coordinates",
            "normalized_xyxy",
        ]
    )

    assert live.rgb_fire_verifier_model == Path("verifier.engine")
    assert live.rgb_fire_verifier_model_manifest == Path("verifier.json")
    assert live.rgb_fire_verifier_class_names == "fire,smoke"
    assert live.rgb_fire_verifier_confidence_threshold == 0.70
    assert live.rgb_fire_verifier_minimum_iou == 0.40
    assert live.rgb_fire_verifier_output_coordinates == "normalized_xyxy"


def test_live_monocular_avoidance_cli_remains_advisory_only() -> None:
    parser = cli_module.build_parser()

    live = parser.parse_args(
        [
            "live-camera",
            str(CONFIG),
            "--onnx-model",
            "model.onnx",
            "--class-names",
            "fire,smoke",
            "--monocular-avoidance",
            "--avoidance-avoid-ttc-seconds",
            "1.25",
        ]
    )

    assert live.monocular_avoidance is True
    assert live.avoidance_avoid_ttc_seconds == 1.25


def test_live_unified_target_pool_and_reid_cli_are_explicit() -> None:
    parser = cli_module.build_parser()

    live = parser.parse_args(
        [
            "live-camera",
            str(CONFIG),
            "--onnx-model",
            "model.onnx",
            "--unified-target-pool",
            "--unified-target-pool-maximum-tracks",
            "32",
            "--unified-target-pool-locked-reacquisition-seconds",
            "6.0",
            "--unified-target-pool-minimum-association-confidence",
            "0.12",
            "--unified-target-pool-priority-minimum-new-track-confidence",
            "0.28",
            "--unified-target-pool-minimum-new-track-confidence",
            "0.40",
            "--unified-target-pool-high-confidence-threshold",
            "0.60",
            "--unified-target-pool-person-maximum-appearance-distance",
            "0.70",
            "--unified-target-pool-person-strict-reid-distance",
            "0.22",
            "--unified-target-pool-kalman-process-noise",
            "0.02",
            "--unified-target-pool-kalman-measurement-noise",
            "0.0002",
            "--unified-target-pool-kalman-gate-sigma",
            "3.5",
            "--unified-target-pool-kalman-maximum-horizon-seconds",
            "1.5",
            "--patrol-advisory",
            "--person-reid-onnx",
            "person.onnx",
            "--person-reid-engine",
            "person.engine",
            "--vehicle-reid-onnx",
            "vehicle.onnx",
            "--vehicle-reid-engine",
            "vehicle.engine",
            "--vehicle-reid-maximum-batch-size",
            "4",
            "--person-reid-frame-stride",
            "3",
            "--vehicle-reid-frame-stride",
            "4",
            "--reid-maximum-interval-seconds",
            "0.2",
        ]
    )

    assert live.unified_target_pool is True
    assert live.unified_target_pool_maximum_tracks == 32
    assert live.unified_target_pool_locked_reacquisition_seconds == 6.0
    assert live.unified_target_pool_minimum_association_confidence == 0.12
    assert live.unified_target_pool_priority_minimum_new_track_confidence == 0.28
    assert live.unified_target_pool_minimum_new_track_confidence == 0.40
    assert live.unified_target_pool_high_confidence_threshold == 0.60
    assert live.unified_target_pool_person_maximum_appearance_distance == 0.70
    assert live.unified_target_pool_person_strict_reid_distance == 0.22
    assert live.unified_target_pool_kalman_process_noise == 0.02
    assert live.unified_target_pool_kalman_measurement_noise == 0.0002
    assert live.unified_target_pool_kalman_gate_sigma == 3.5
    assert live.unified_target_pool_kalman_maximum_horizon_seconds == 1.5
    assert live.patrol_advisory is True
    assert live.person_reid_onnx == Path("person.onnx")
    assert live.person_reid_engine == Path("person.engine")
    assert live.vehicle_reid_onnx == Path("vehicle.onnx")
    assert live.vehicle_reid_engine == Path("vehicle.engine")
    assert live.vehicle_reid_maximum_batch_size == 4
    assert live.person_reid_frame_stride == 3
    assert live.vehicle_reid_frame_stride == 4
    assert live.reid_maximum_interval_seconds == 0.2


def test_live_short_term_tracking_cli_is_explicit_and_metadata_only() -> None:
    parser = cli_module.build_parser()

    live = parser.parse_args(
        [
            "live-camera",
            str(CONFIG),
            "--onnx-model",
            "model.onnx",
            "--unified-target-pool",
            "--short-term-tracking",
            "--short-term-maximum-tracks",
            "12",
            "--short-term-search-expansion",
            "2.25",
            "--short-term-occluded-search-multiplier",
            "1.75",
            "--short-term-reacquiring-search-multiplier",
            "2.25",
            "--short-term-maximum-search-expansion",
            "5.5",
            "--short-term-maximum-retained-template-age-seconds",
            "1.8",
        ]
    )

    assert live.unified_target_pool is True
    assert live.short_term_tracking is True
    assert live.short_term_maximum_tracks == 12
    assert live.short_term_search_expansion == 2.25
    assert live.short_term_occluded_search_multiplier == 1.75
    assert live.short_term_reacquiring_search_multiplier == 2.25
    assert live.short_term_maximum_search_expansion == 5.5
    assert live.short_term_maximum_retained_template_age_seconds == 1.8


def test_live_multimodal_ranging_cli_is_explicit_and_read_only() -> None:
    parser = cli_module.build_parser()

    live = parser.parse_args(
        [
            "live-camera",
            str(CONFIG),
            "--onnx-model",
            "model.onnx",
            "--pixhawk-endpoint",
            "udp:0.0.0.0:14550",
            "--unified-target-pool",
            "--multimodal-ranging",
            "--ranging-calibration",
            "camera.json",
            "--ranging-agl-sigma-m",
            "2.0",
        ]
    )

    assert live.multimodal_ranging is True
    assert live.ranging_calibration == Path("camera.json")
    assert live.ranging_agl_sigma_m == 2.0


def test_live_fixed_wing_aim_control_cli_has_bounded_real_control_defaults() -> None:
    parser = cli_module.build_parser()

    live = parser.parse_args(
        [
            "live-camera",
            str(CONFIG),
            "--onnx-model",
            "model.onnx",
            "--fixed-wing-aim-control",
            "--aim-prestream-setpoints",
            "12",
            "--aim-control-mode",
            "OFFBOARD",
            "--aim-return-mode",
            "AUTO",
        ]
    )

    assert live.fixed_wing_aim_control is True
    assert live.aim_minimum_airspeed_mps == 12.0
    assert live.aim_maximum_abs_roll_deg == 20.0
    assert live.aim_maximum_abs_pitch_deg == 15.0
    assert live.aim_prestream_setpoints == 12
    assert live.aim_control_mode == "OFFBOARD"
    assert live.aim_return_mode == "AUTO"
    assert live.aim_rc_input_rate_hz == 20.0
    assert live.aim_rc_input_maximum_age_seconds == 0.30
    assert live.aim_rc_cancel_threshold_us == 50


def test_live_fixed_wing_aim_control_requires_mode3_and_pixhawk(capsys) -> None:
    base = ["live-camera", str(CONFIG), "--onnx-model", "model.onnx"]

    assert main([*base, "--fixed-wing-aim-control"]) == 1
    missing = json.loads(capsys.readouterr().err)["message"]
    assert "--mode3-aim" in missing
    assert "--pixhawk-endpoint" in missing


def test_live_payload_target_hil_requires_explicit_safe_dependencies(capsys) -> None:
    base = ["live-camera", str(CONFIG), "--onnx-model", "model.onnx"]

    assert main([*base, "--payload-target-hil"]) == 1
    missing = json.loads(capsys.readouterr().err)["message"]
    assert "--operator-udp-port" in missing
    assert "--unified-target-pool" in missing
    assert "--rgb-fire-verifier-model" in missing

    assert (
        main(
            [
                *base,
                "--operator-udp-port",
                "14561",
                "--unified-target-pool",
                "--payload-target-hil",
            ]
        )
        == 1
    )
    assert "requires --rgb-fire-verifier-model" in json.loads(capsys.readouterr().err)["message"]


def test_mode2_payload_target_and_mode3_approach_hil_are_mutually_exclusive(capsys) -> None:
    result = main(
        [
            "live-camera",
            str(CONFIG),
            "--onnx-model",
            "model.onnx",
            "--operator-udp-port",
            "14561",
            "--unified-target-pool",
            "--rgb-fire-verifier-model",
            "independent-fire.onnx",
            "--payload-target-hil",
            "--monocular-avoidance",
            "--pixhawk-endpoint",
            "udp:0.0.0.0:14550",
            "--multimodal-ranging",
            "--ranging-calibration",
            "camera.json",
            "--approach-hil",
        ]
    )

    assert result == 1
    assert "mutually exclusive" in json.loads(capsys.readouterr().err)["message"]


def test_live_multimodal_ranging_requires_pool_pixhawk_and_calibration(capsys) -> None:
    base = ["live-camera", str(CONFIG), "--onnx-model", "model.onnx"]

    assert main([*base, "--multimodal-ranging"]) == 1
    assert "requires --unified-target-pool" in json.loads(capsys.readouterr().err)["message"]

    assert main([*base, "--unified-target-pool", "--multimodal-ranging"]) == 1
    assert "requires --pixhawk-endpoint" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                *base,
                "--unified-target-pool",
                "--pixhawk-endpoint",
                "udp:0.0.0.0:14550",
                "--multimodal-ranging",
            ]
        )
        == 1
    )
    assert "requires --ranging-calibration" in json.loads(capsys.readouterr().err)["message"]


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
                "--heading-deg",
                "0",
                "--velocity-north-mps",
                "18",
                "--velocity-east-mps",
                "0",
                "--airspeed-mps",
                "16",
                "--wind-north-mps",
                "2",
                "--wind-east-mps",
                "0",
                "--target-north-m",
                "45.1",
                "--target-east-m",
                "0",
                "--range-ci-low-m",
                "44.5",
                "--range-ci-high-m",
                "45.7",
                "--bearing-sigma-deg",
                "0.5",
                "--range-sensor-consistency",
                "0.9",
                "--range-calibration-id",
                "camera-hil-v1",
                "--now-s",
                "100",
            ]
        )
        == 0
    )

    output = parsed_stdout(capsys)[-1]
    assert output["event"] == "fixed_wing_release_window_checked"
    assert output["status"] == "ready"
    assert output["timing_status"] == "window"
    assert output["error_ellipse_major_m"] > 0.0
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
            "--heading-deg",
            "0",
            "--velocity-north-mps",
            "18",
            "--velocity-east-mps",
            "0",
            "--airspeed-mps",
            "16",
            "--wind-north-mps",
            "2",
            "--wind-east-mps",
            "0",
            "--target-north-m",
            "45.1",
            "--target-east-m",
            "0",
            "--range-ci-low-m",
            "44.5",
            "--range-ci-high-m",
            "45.7",
            "--bearing-sigma-deg",
            "0.5",
            "--range-sensor-consistency",
            "0.9",
            "--range-calibration-id",
            "camera-hil-v1",
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

    monkeypatch.setattr(cli_module, "frame_source_from_config", _Source)

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
    patrol = next(item for item in output if item["event"] == "g20_patrol_status_received")
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
    assert patrol["payload_bytes"] == 110
    assert patrol["phase"] == "lost"
    assert patrol["target_state"] == "lost"
    assert patrol["total_track_count"] == 10
    assert patrol["locked_track_count"] == 2
    assert patrol["return_direction"] == "left"
    assert patrol["return_validity"] == "degraded"
    assert patrol["advisory_only"] is True
    assert patrol["flight_control_enabled"] is False
    assert finished["selection_delivered"] is True
    assert finished["tracking_status_received"] is True
    assert finished["mission_status_received"] is True
    assert finished["safety_status_received"] is True
    assert finished["patrol_status_received"] is True
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


def test_model_manifest_init_records_raw_yolo_native_output(tmp_path: Path, capsys) -> None:
    engine = tmp_path / "common.engine"
    manifest = tmp_path / "common.manifest.json"
    engine.write_bytes(b"raw-yolo-engine")

    assert (
        main(
            [
                "model-manifest-init",
                "--onnx-model",
                str(engine),
                "--out",
                str(manifest),
                "--model-id",
                "common-raw",
                "--model-version",
                "candidate-v1",
                "--source-description",
                "target-built raw engine",
                "--model-role",
                "safety_object_evidence",
                "--class-names",
                "person,car",
                "--output-coordinates",
                "letterbox_xyxy_px",
                "--native-output-format",
                "ultralytics_raw_xywh_class_scores",
            ]
        )
        == 0
    )

    parsed_stdout(capsys)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["output"]["native_export"]["format"] == ("ultralytics_raw_xywh_class_scores")
    assert document["output"]["native_export"]["nms_embedded"] is False


def test_semantic_manifest_init_creates_categorical_advisory_contract(
    tmp_path: Path,
    capsys,
) -> None:
    model = tmp_path / "city.onnx"
    manifest = tmp_path / "city.manifest.json"
    model.write_bytes(b"semantic-model")

    assert (
        main(
            [
                "semantic-model-manifest-init",
                "--onnx-model",
                str(model),
                "--out",
                str(manifest),
                "--model-id",
                "city-semsegformer",
                "--model-version",
                "deployable-onnx-v1",
                "--source-description",
                "official categorical model",
            ]
        )
        == 0
    )

    document = json.loads(manifest.read_text(encoding="utf-8"))
    created = parsed_stdout(capsys)[-1]
    assert document["model_role"] == "semantic_scene_context"
    assert document["output"]["adapter_contract"]["format"] == "categorical_H_W_1"
    assert document["output"]["adapter_contract"]["confidence_available"] is False
    assert len(document["classes"]) == 19
    assert created["confidence_available"] is False
    assert created["flight_control_enabled"] is False
    assert created["physical_release_enabled"] is False


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


def test_live_person_reid_requires_target_pool_and_source_onnx(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--person-reid-onnx",
                "person.onnx",
            ]
        )
        == 1
    )
    assert "requires --unified-target-pool" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--unified-target-pool",
                "--person-reid-engine",
                "person.engine",
            ]
        )
        == 1
    )
    assert "requires --person-reid-onnx" in json.loads(capsys.readouterr().err)["message"]


def test_live_vehicle_reid_requires_target_pool_and_source_onnx(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--vehicle-reid-onnx",
                "vehicle.onnx",
            ]
        )
        == 1
    )
    assert "requires --unified-target-pool" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--unified-target-pool",
                "--vehicle-reid-engine",
                "vehicle.engine",
            ]
        )
        == 1
    )
    assert "requires --vehicle-reid-onnx" in json.loads(capsys.readouterr().err)["message"]


def test_live_reid_requires_tensorrt_unless_lab_override_is_explicit(capsys) -> None:
    common = [
        "live-camera",
        str(PATROL_CONFIG),
        "--onnx-model",
        "unused.onnx",
        "--unified-target-pool",
    ]

    assert main([*common, "--person-reid-onnx", "person.onnx"]) == 1
    assert "requires --person-reid-engine" in json.loads(capsys.readouterr().err)["message"]

    assert main([*common, "--vehicle-reid-onnx", "vehicle.onnx"]) == 1
    assert "requires --vehicle-reid-engine" in json.loads(capsys.readouterr().err)["message"]

    assert main([*common, "--allow-nonrealtime-reid"]) == 1
    assert (
        "requires a ReID ONNX without its TensorRT engine"
        in json.loads(capsys.readouterr().err)["message"]
    )

    assert (
        main(
            [
                *common,
                "--person-reid-onnx",
                "person.onnx",
                "--person-reid-engine",
                "person.engine",
                "--allow-nonrealtime-reid",
            ]
        )
        == 1
    )
    assert (
        "requires a ReID ONNX without its TensorRT engine"
        in json.loads(capsys.readouterr().err)["message"]
    )


def test_live_nonrealtime_reid_override_is_parse_visible() -> None:
    args = cli_module.build_parser().parse_args(
        [
            "live-camera",
            str(PATROL_CONFIG),
            "--onnx-model",
            "unused.onnx",
            "--unified-target-pool",
            "--person-reid-onnx",
            "person.onnx",
            "--allow-nonrealtime-reid",
        ]
    )

    assert args.allow_nonrealtime_reid is True


def test_live_vehicle_reid_batch_limit_is_validated(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--unified-target-pool",
                "--vehicle-reid-maximum-batch-size",
                "11",
            ]
        )
        == 1
    )
    assert "must be between 1 and 10" in json.loads(capsys.readouterr().err)["message"]


@pytest.mark.parametrize(
    ("option", "value", "expected"),
    [
        ("--person-reid-frame-stride", "0", "between 1 and 30"),
        ("--vehicle-reid-frame-stride", "31", "between 1 and 30"),
        ("--reid-maximum-interval-seconds", "2.1", "between 0.01 and 2"),
    ],
)
def test_live_reid_cadence_limits_are_validated(
    option: str,
    value: str,
    expected: str,
    capsys,
) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                option,
                value,
            ]
        )
        == 1
    )
    assert expected in json.loads(capsys.readouterr().err)["message"]


def test_live_short_term_tracking_requires_target_pool(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--short-term-tracking",
            ]
        )
        == 1
    )
    assert "requires --unified-target-pool" in json.loads(capsys.readouterr().err)["message"]


def test_live_identity_tracking_log_requires_target_pool(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--identity-tracking-log-out",
                "identity-tracks.jsonl",
            ]
        )
        == 1
    )
    assert "requires --unified-target-pool" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--unified-target-pool",
                "--identity-tracking-log-out",
                "identity-tracks.jsonl",
            ]
        )
        == 1
    )
    assert (
        "requires --identity-tracking-session-id" in json.loads(capsys.readouterr().err)["message"]
    )

    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--unified-target-pool",
                "--identity-tracking-session-id",
                "12345678-1234-5678-9234-567812345678",
            ]
        )
        == 1
    )
    assert "requires --identity-tracking-log-out" in json.loads(capsys.readouterr().err)["message"]

    parsed = cli_module.build_parser().parse_args(
        [
            "live-camera",
            str(PATROL_CONFIG),
            "--onnx-model",
            "unused.onnx",
            "--unified-target-pool",
            "--identity-tracking-log-out",
            "identity-tracks.jsonl",
            "--identity-tracking-session-id",
            "12345678-1234-5678-9234-567812345678",
        ]
    )
    assert parsed.identity_tracking_log_out == Path("identity-tracks.jsonl")
    assert parsed.identity_tracking_session_id == "12345678-1234-5678-9234-567812345678"


def test_live_patrol_advisory_requires_target_pool(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "unused.onnx",
                "--patrol-advisory",
            ]
        )
        == 1
    )
    assert "requires --unified-target-pool" in json.loads(capsys.readouterr().err)["message"]


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


def test_live_rejects_wrong_role_or_protected_environment_labels(
    tmp_path, monkeypatch, capsys
) -> None:
    model = tmp_path / "candidate.onnx"
    model.write_bytes(b"environment-role-gate")
    wrong_manifest = write_candidate_model_manifest(
        tmp_path / "wrong.manifest.json",
        create_candidate_model_manifest(
            model,
            model_id="common-object-candidate",
            model_version="v1",
            class_names=("building", "road", "power_line", "flammable_tank"),
            input_width=640,
            input_height=640,
            output_coordinates="normalized_xyxy",
            source_description="test wrong environment role",
            model_role="safety_object_evidence",
        ),
    )
    monkeypatch.setattr(cli_module, "OnnxNx6Detector", lambda _config: None)

    common = [
        "live-camera",
        str(PATROL_CONFIG),
        "--onnx-model",
        str(model),
        "--environment-onnx-model",
        str(model),
        "--environment-output-coordinates",
        "normalized_xyxy",
    ]
    assert main([*common, "--environment-model-manifest", str(wrong_manifest)]) == 1
    error = json.loads(capsys.readouterr().err)
    assert "safety_object_evidence" in error["message"]
    assert "environment_risk_evidence" in error["message"]

    assert main([*common, "--environment-class-names", "building,person"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert "protected fire/person/vehicle domains" in error["message"]


def test_live_requires_manifest_for_environment_tensorrt_engine(capsys) -> None:
    assert (
        main(
            [
                "live-camera",
                str(PATROL_CONFIG),
                "--onnx-model",
                "fire.onnx",
                "--environment-onnx-model",
                "environment.engine",
            ]
        )
        == 1
    )
    assert (
        "hash-bound --environment-model-manifest" in json.loads(capsys.readouterr().err)["message"]
    )


def test_live_semantic_context_requires_bound_onnx_and_positive_timing(capsys) -> None:
    base = ["live-camera", str(PATROL_CONFIG), "--onnx-model", "fire.onnx"]

    assert main([*base, "--semantic-context-onnx-model", "city.onnx"]) == 1
    assert "hash-bound" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                *base,
                "--semantic-context-onnx-model",
                "city.engine",
                "--semantic-context-model-manifest",
                "city.json",
            ]
        )
        == 1
    )
    assert "requires an ONNX artifact" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                *base,
                "--semantic-context-minimum-interval-seconds",
                "0",
            ]
        )
        == 1
    )
    assert "finite and positive" in json.loads(capsys.readouterr().err)["message"]

    assert main([*base, "--semantic-context-engine", "city.engine"]) == 1
    assert "must be supplied together" in json.loads(capsys.readouterr().err)["message"]

    assert (
        main(
            [
                *base,
                "--semantic-context-engine",
                "city.engine",
                "--semantic-context-engine-provenance",
                "city.provenance.json",
            ]
        )
        == 1
    )
    assert (
        "requires --semantic-context-onnx-model" in json.loads(capsys.readouterr().err)["message"]
    )


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


def test_evaluate_tracking_command_reports_identity_and_recovery_metrics(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "tracking-evaluation.json"
    assert (
        main(
            [
                "evaluate-tracking",
                str(TRACKING_GROUND_TRUTH),
                str(TRACKING_PREDICTIONS),
                "--dataset-provenance",
                "synthetic_demo",
                "--minimum-idf1",
                "0.9",
                "--maximum-id-switch-count",
                "0",
                "--minimum-occlusion-recovery-rate",
                "0.9",
                "--minimum-out-of-frame-recovery-rate",
                "0.9",
                "--maximum-occlusion-recovery-p95-seconds",
                "0.5",
                "--maximum-out-of-frame-recovery-p95-seconds",
                "2.0",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    assert result["event"] == "identity_tracking_evaluation_completed"
    assert result["dataset_provenance"] == "synthetic_demo"
    assert result["ground_truth_sha256"]
    assert result["predictions_sha256"]
    assert result["source_video_sha256"] is None
    assert result["annotations_reviewed"] is False
    assert result["deployment_domain_evidence_complete"] is False
    assert result["acceptance_evaluated"] is True
    assert result["passed"] is True
    assert result["failure_reasons"] == []
    assert result["overall"]["idf1"] == pytest.approx(1.0)
    assert result["overall"]["id_switch_count"] == 0
    assert result["occlusion_recovery"]["recovery_rate"] == pytest.approx(1.0)
    assert result["out_of_frame_recovery"]["recovery_rate"] == pytest.approx(1.0)
    assert result["flight_control_enabled"] is False
    assert result["physical_release_enabled"] is False
    assert json.loads(output.read_text(encoding="utf-8")) == result


def test_evaluate_tracking_command_returns_nonzero_when_identity_gate_fails(
    tmp_path: Path,
    capsys,
) -> None:
    switched = tmp_path / "switched.jsonl"
    records = [
        json.loads(line) for line in TRACKING_PREDICTIONS.read_text(encoding="utf-8").splitlines()
    ]
    for frame_index in (3, 5):
        records[frame_index]["tracks"][0]["track_id"] = "target-000099"
    switched.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "evaluate-tracking",
                str(TRACKING_GROUND_TRUTH),
                str(switched),
                "--maximum-id-switch-count",
                "0",
            ]
        )
        == 2
    )
    result = parsed_stdout(capsys)[-1]
    assert result["passed"] is False
    assert result["overall"]["id_switch_count"] == 1
    assert result["failure_reasons"] == ["identity switch count exceeds the configured maximum"]


def test_unified_tracking_bench_is_hardware_free_and_writes_metrics(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "unified-tracking-bench.json"

    assert (
        main(
            [
                "unified-tracking-bench",
                "--track-count",
                "10",
                "--benchmark-frames",
                "60",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    written = json.loads(output.read_text(encoding="utf-8"))
    assert result == written
    assert result["event"] == "unified_tracking_core_benchmark_completed"
    assert result["passed"] is True
    assert result["track_count"] == 10
    assert result["benchmark_frame_count"] == 60
    assert result["measured_end_to_end_metadata_rate_hz"] >= 15.0
    assert result["association_latency_p99_ms"] > 0.0
    assert result["repeated_switch_latency_p95_ms"] <= 200.0
    assert result["camera_opened"] is False
    assert result["model_inference_executed"] is False
    assert result["pixhawk_opened"] is False
    assert result["flight_control_enabled"] is False
    assert result["physical_release_enabled"] is False


def test_patrol_reacquisition_sitl_is_isolated_receive_only_and_camera_free(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    output = tmp_path / "patrol-reacquisition-sitl.json"

    class _Qualification:
        passed = True

        @staticmethod
        def to_document() -> dict[str, object]:
            return {"required": True, "passed": True, "reasons": []}

    class _Provider:
        is_read_only = True
        messages_transmitted = 0
        qualification = _Qualification()

        def __init__(self, config) -> None:
            assert config.endpoint == "udpin:127.0.0.1:14652"

        def snapshot(self, *, now_s: float) -> VehicleTelemetry:
            assert now_s > 0.0
            return VehicleTelemetry(
                altitude_agl_m=50.0,
                roll_deg=0.0,
                pitch_deg=0.0,
                ground_speed_mps=20.0,
                in_allowed_zone=None,
                geofence_healthy=None,
                position_healthy=True,
                link_healthy=True,
                flight_mode_allows_deploy=None,
                release_zone_clear=None,
                armed=True,
                flight_mode="MISSION",
                mission_sequence=2,
            )

        def diagnostics(self, *, now_s: float) -> dict[str, object]:
            assert now_s > 0.0
            return {
                "configured_endpoint": "udpin:127.0.0.1:14652",
                "read_only": True,
                "messages_transmitted": 0,
                "qualification": self.qualification.to_document(),
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "PixhawkReadOnlyTelemetryProvider", _Provider)

    assert (
        main(
            [
                "patrol-reacquisition-sitl",
                "--endpoint",
                "udpin:127.0.0.1:14652",
                "--samples",
                "2",
                "--interval-seconds",
                "0",
                "--acknowledge-owned-disposable-sitl",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert result["event"] == "patrol_reacquisition_sitl_acceptance_completed"
    assert result["passed"] is True
    assert all(result["requirements"].values())
    assert result["scope"]["camera_opened"] is False
    assert result["scope"]["network_camera_contacted"] is False
    assert result["scope"]["application_mavlink_messages_transmitted"] == 0
    assert result["scenario"]["track_count"] == 10
    assert result["scenario"]["return_to_observe"]["validity"] == "degraded"
    assert result["scenario"]["flight_control_enabled"] is False


def test_patrol_reacquisition_sitl_requires_opt_in_and_refuses_qgc_port(capsys) -> None:
    base = [
        "patrol-reacquisition-sitl",
        "--endpoint",
        "udpin:0.0.0.0:14652",
        "--out",
        "unused.json",
    ]
    assert main(base) == 1
    assert "acknowledge-owned-disposable-sitl" in json.loads(capsys.readouterr().err)["message"]

    protected = [
        "patrol-reacquisition-sitl",
        "--endpoint",
        "udpin:0.0.0.0:14550",
        "--acknowledge-owned-disposable-sitl",
        "--out",
        "unused.json",
    ]
    assert main(protected) == 1
    assert (
        "refuses protected ground-station UDP 14550"
        in json.loads(capsys.readouterr().err)["message"]
    )


def test_short_term_tracking_bench_exercises_image_recovery_without_hardware(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "short-term-image-bench.json"

    assert (
        main(
            [
                "short-term-tracking-bench",
                "--benchmark-frames",
                "60",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert result["event"] == "short_term_image_tracking_benchmark_completed"
    assert result["passed"] is True
    assert result["track_count"] == 10
    assert result["retained_template_recovery_hint_observed"] is True
    assert result["recovered_same_track_id"] is True
    assert result["recovery_s"] <= 0.5
    assert result["processing_latency_p95_ms"] <= 66.7
    assert result["processed_update_rate_hz"] == pytest.approx(15.0)
    assert result["camera_opened"] is False
    assert result["model_inference_executed"] is False
    assert result["pixhawk_opened"] is False
    assert result["flight_control_enabled"] is False
    assert result["physical_release_enabled"] is False


def test_monocular_avoidance_bench_exercises_image_risk_without_hardware(
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "monocular-avoidance-image-bench.json"

    assert (
        main(
            [
                "monocular-avoidance-bench",
                "--benchmark-frames",
                "60",
                "--out",
                str(output),
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert result["event"] == "monocular_avoidance_image_benchmark_completed"
    assert result["passed"] is True
    assert result["static_scene_state"] == "clear"
    assert result["camera_translation_state"] == "clear"
    assert result["approaching_obstacle_state"] == "avoid"
    assert result["approaching_center_zone_state"] == "avoid"
    assert result["stale_evidence_state"] == "invalid"
    assert result["processing_latency_p95_ms"] <= 66.7
    assert result["camera_opened"] is False
    assert result["model_inference_executed"] is False
    assert result["pixhawk_opened"] is False
    assert result["all_outputs_advisory_only"] is True
    assert result["flight_control_enabled"] is False
    assert result["physical_release_enabled"] is False


def test_reid_onnx_cpu_bench_writes_domain_and_runtime_boundaries(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    output = tmp_path / "reid-cpu-bench.json"

    def _run(config):
        assert config.person_count == 4
        assert config.vehicle_count == 4
        return {
            "person_embedding_count": 4,
            "vehicle_embedding_count": 4,
            "identity_domains_disjoint": True,
            "realtime_budget_passed": False,
            "deployment_domain_accuracy_validated": False,
            "target_tensorrt_runtime_validated": False,
            "camera_opened": False,
            "pixhawk_opened": False,
            "flight_control_enabled": False,
            "physical_release_enabled": False,
        }

    monkeypatch.setattr(cli_module, "run_reid_model_acceptance", _run)
    assert (
        main(
            [
                "reid-onnx-cpu-bench",
                "--person-model",
                str(tmp_path / "person.onnx"),
                "--vehicle-model",
                str(tmp_path / "vehicle.onnx"),
                "--out",
                str(output),
            ]
        )
        == 0
    )

    result = parsed_stdout(capsys)[-1]
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert result["event"] == "reid_onnx_cpu_benchmark_completed"
    assert result["passed"] is True
    assert result["identity_domains_disjoint"] is True
    assert result["realtime_budget_passed"] is False
    assert result["deployment_domain_accuracy_validated"] is False
    assert result["target_tensorrt_runtime_validated"] is False
    assert result["flight_control_enabled"] is False
    assert result["physical_release_enabled"] is False


@pytest.mark.parametrize(("realtime_passed", "expected_exit"), ((True, 0), (False, 2)))
def test_reid_tensorrt_bench_is_a_target_runtime_gate(
    realtime_passed: bool,
    expected_exit: int,
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    output = tmp_path / "reid-tensorrt-bench.json"

    def _run(config):
        assert config.person_model_path == tmp_path / "person.onnx"
        assert config.vehicle_model_path == tmp_path / "vehicle.onnx"
        assert config.person_engine_path == tmp_path / "person.engine"
        assert config.vehicle_engine_path == tmp_path / "vehicle.engine"
        assert config.iterations == 20
        return {
            "target_tensorrt_runtime_validated": True,
            "repeat_stability_validated": True,
            "realtime_budget_passed": realtime_passed,
            "identity_domains_disjoint": True,
            "deployment_domain_accuracy_validated": False,
            "camera_opened": False,
            "pixhawk_opened": False,
            "flight_control_enabled": False,
            "physical_release_enabled": False,
        }

    monkeypatch.setattr(cli_module, "run_reid_tensorrt_acceptance", _run)
    assert (
        main(
            [
                "reid-tensorrt-bench",
                "--person-model",
                str(tmp_path / "person.onnx"),
                "--vehicle-model",
                str(tmp_path / "vehicle.onnx"),
                "--person-engine",
                str(tmp_path / "person.engine"),
                "--vehicle-engine",
                str(tmp_path / "vehicle.engine"),
                "--out",
                str(output),
            ]
        )
        == expected_exit
    )

    result = parsed_stdout(capsys)[-1]
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert result["event"] == "reid_tensorrt_benchmark_completed"
    assert result["passed"] is realtime_passed
    assert result["target_tensorrt_runtime_validated"] is True
    assert result["deployment_domain_accuracy_validated"] is False
    assert result["camera_opened"] is False
    assert result["pixhawk_opened"] is False
    assert result["flight_control_enabled"] is False
    assert result["physical_release_enabled"] is False


def test_prepare_tracking_review_command_creates_unreviewed_hash_bound_bundle(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    session_id = "12345678-1234-5678-9234-567812345678"
    predictions = tmp_path / "identity-tracks.jsonl"
    prediction_records = [
        json.loads(line) for line in TRACKING_PREDICTIONS.read_text(encoding="utf-8").splitlines()
    ]
    for record in prediction_records:
        record["session_id"] = session_id
    predictions.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in prediction_records)
        + "\n",
        encoding="utf-8",
    )
    video = tmp_path / "source.mp4"
    video.write_bytes(b"source-video")
    source_video_manifest = tmp_path / "source.manifest.json"
    source_video_manifest.write_text(
        json.dumps(
            {
                "event": "rtsp_tracking_evidence_recording_completed",
                "schema_version": 2,
                "session_id": session_id,
                "source_uri_recorded": False,
                "stream_copy_no_decode_or_reencode": True,
                "output_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                "output_bytes": video.stat().st_size,
                "started_at_monotonic_s": 0.0,
                "ended_at_monotonic_s": 1.0,
                "passed": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "review"
    monkeypatch.setattr(
        tracking_review_module,
        "probe_video_evidence",
        lambda path: VideoEvidenceProbe(
            path=Path(path),
            decoded_frame_count=12,
            declared_frame_count=12,
            fps=12.0,
            width=1280,
            height=720,
            duration_s=1.0,
            full_frame_scan_completed=True,
            stable_dimensions=True,
            passed=True,
            failure_reasons=(),
        ),
    )

    assert (
        main(
            [
                "prepare-tracking-review",
                str(predictions),
                str(video),
                str(source_video_manifest),
                str(output),
            ]
        )
        == 0
    )
    result = parsed_stdout(capsys)[-1]
    manifest = json.loads((output / "review-manifest.json").read_text(encoding="utf-8"))
    assert result["event"] == "tracking_identity_review_bundle_prepared"
    assert result["review_status"] == "pending"
    assert result["annotations_reviewed"] is False
    assert result["deployment_domain_evidence_complete"] is False
    assert result["draft_is_evaluation_input"] is False
    assert result["source_video_media_decoding_validated"] is True
    assert result["source_video_track_timeline_coverage_validated"] is True
    assert result["video_frame_alignment_reviewed"] is False
    assert result["evidence_session_id"] == session_id
    assert result["session_id_binding_validated"] is True
    assert result["monotonic_recording_window_validated"] is True
    assert result["source_video_sha256"] == manifest["source_video_sha256"]
    assert result["predictions_sha256"] == manifest["predictions_sha256"]
    assert result["manifest_path"] == str(output / "review-manifest.json")


def test_record_rtsp_evidence_command_remains_stream_copy_and_control_free(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    output = tmp_path / "recording.mkv"
    manifest = tmp_path / "recording.manifest.json"
    probe = VideoEvidenceProbe(
        path=output,
        decoded_frame_count=250,
        declared_frame_count=250,
        fps=25.0,
        width=1280,
        height=720,
        duration_s=10.0,
        full_frame_scan_completed=True,
        stable_dimensions=True,
        passed=True,
        failure_reasons=(),
    )
    captured_configs = []

    def fake_record(config):
        captured_configs.append(config)
        return RtspEvidenceRecordingReport(
            session_id="12345678-1234-5678-9234-567812345678",
            output_video=output,
            manifest_out=manifest,
            requested_duration_s=10.0,
            actual_duration_s=10.1,
            started_at_monotonic_s=100.0,
            ended_at_monotonic_s=110.1,
            output_bytes=1234,
            output_sha256="a" * 64,
            video_probe=probe,
            eos_received=True,
            passed=True,
        )

    monkeypatch.setattr(cli_module, "record_rtsp_evidence", fake_record)
    assert (
        main(
            [
                "record-rtsp-evidence",
                "--source-env",
                "CAMERA_SOURCE",
                "--session-id",
                "12345678-1234-5678-9234-567812345678",
                "--out-video",
                str(output),
                "--manifest-out",
                str(manifest),
                "--duration-seconds",
                "10",
            ]
        )
        == 0
    )
    result = parsed_stdout(capsys)[-1]
    assert captured_configs[0].source_env == "CAMERA_SOURCE"
    assert captured_configs[0].session_id == "12345678-1234-5678-9234-567812345678"
    assert result["source_uri_recorded"] is False
    assert result["stream_copy_no_decode_or_reencode"] is True
    assert result["passed"] is True
    assert result["flight_control_enabled"] is False
    assert result["physical_release_enabled"] is False


def test_record_rtsp_evidence_missing_source_is_not_labeled_simulation(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.delenv("MISSING_CAMERA_SOURCE", raising=False)
    assert (
        main(
            [
                "record-rtsp-evidence",
                "--source-env",
                "MISSING_CAMERA_SOURCE",
                "--session-id",
                "12345678-1234-5678-9234-567812345678",
                "--out-video",
                str(tmp_path / "recording.mkv"),
                "--manifest-out",
                str(tmp_path / "manifest.json"),
            ]
        )
        == 1
    )
    error = json.loads(capsys.readouterr().err)
    assert error["simulation_only"] is False
    assert error["hardware_control_enabled"] is False

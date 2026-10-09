from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.mission_journal import (
    DurableMissionJournal,
    MissionJournalIdentity,
    MissionTransactionState,
)
from multidetect.px4_gazebo_qgc_router import PX4_AUTO_MISSION_CUSTOM_MODE
from multidetect.px4_mission import (
    MissionTransactionBindings,
    Px4MissionItemInt,
    px4_wire_hash,
)
from scripts import run_vtol_mission_sensor_capture as capture
from scripts.run_simulator_docker import build_docker_simulator_plan


def _energy_configuration():
    return dict(
        capacity_wh=100.0,
        avionics_power_w=36.0,
        propulsion_max_power_w=324.0,
        initial_remaining_wh=90.0,
        provenance="synthetic-test",
    )


def test_energy_options_roundtrip_to_child_parser():
    from scripts.vtol_sitl_mission_acceptance import _parser

    options = capture._energy_arguments(_energy_configuration())
    args, remaining = _parser().parse_known_args(
        [
            "--evidence",
            "test.json",
            "--container-name",
            "test",
            "--expected-container-id",
            "a" * 64,
            "--evaluator-port",
            "14691",
            "--airframe-profile-sha256",
            "b" * 64,
            "--runtime-descriptor-sha256",
            "c" * 64,
            "--runtime-provenance-sha256",
            "d" * 64,
            *options,
        ]
    )
    assert remaining == []
    for key, value in _energy_configuration().items():
        assert getattr(args, "energy_" + key) == value


@pytest.mark.parametrize(
    "key,value",
    [
        ("capacity_wh", None),
        ("capacity_wh", float("nan")),
        ("initial_remaining_wh", 101.0),
        ("initial_remaining_wh", -1.0),
        ("provenance", ""),
        ("propulsion_max_power_w", -1.0),
    ],
)
def test_bad_energy_configuration_rejected_before_paths_or_docker(tmp_path, key, value):
    config = _energy_configuration()
    config[key] = value
    directory = tmp_path / "must-not-exist"
    with pytest.raises((ValueError, capture.VtolMissionSensorCaptureError)):
        capture.run_vtol_mission_sensor_capture(
            run_directory=directory,
            energy_configuration=config,
            command_runner=lambda *a, **kw: pytest.fail("Docker accessed"),
        )
    assert not directory.exists()


def test_parent_cli_transfers_energy_configuration(monkeypatch):
    seen = []
    monkeypatch.setattr(
        capture,
        "run_vtol_mission_sensor_capture",
        lambda *a, **kw: seen.append(kw) or {"gate_passed": False},
    )
    assert capture.main(capture._energy_arguments(_energy_configuration())) == 0
    assert seen[0]["energy_configuration"] == _energy_configuration()


def test_sim_energy_profile_is_explicit_and_never_live_endurance() -> None:
    configuration = capture._resolve_energy_configuration(
        None, capture.SIM_ENERGY_PROFILE_ID
    )
    assert configuration is not None
    assert configuration["capacity_wh"] == 100.0
    assert configuration["initial_remaining_wh"] == 100.0
    assert "uncalibrated SIM-only" in configuration["provenance"]
    assert "not BATTERY_STATUS" in configuration["provenance"]
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="cannot be combined"):
        capture._resolve_energy_configuration(
            _energy_configuration(), capture.SIM_ENERGY_PROFILE_ID
        )
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="unknown SIM"):
        capture._resolve_energy_configuration(None, "not-a-profile")


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json"
CONTAINER_ID = "d" * 64
DESCRIPTOR_HASH = "a" * 64
PROVENANCE_HASH = "b" * 64
SCENARIO_HASH = "e" * 64
AIRFRAME_HASH = "1" * 64
READY_MONOTONIC_S = 1_000.0
READY_PATH = (ROOT / "artifacts" / "test-vtol-flight-capture-ready.json").resolve()
COMPLETE_PATH = (ROOT / "artifacts" / "test-vtol-flight-capture-complete.json").resolve()
COMPLETE_FILE_SHA256 = "f" * 64


def _wire_item(sequence: int) -> dict[str, Any]:
    """Return one strict, deterministic MISSION_ITEM_INT test record."""

    return {
        "target_system": 1,
        "target_component": 1,
        "sequence": sequence,
        "frame": 6,
        "command": 16,
        "current": 1 if sequence == 0 else 0,
        "autocontinue": 1,
        "param1": 0.0,
        "param2": 0.0,
        "param3": 0.0,
        "param4": 0.0,
        "x": 13_000_000 + sequence * 100,
        "y": 1_038_000_000,
        "z": 10.0,
        "mission_type": 0,
    }


WIRE_ITEMS = tuple(_wire_item(sequence) for sequence in range(3))
PROTOCOL_HASH = px4_wire_hash(tuple(Px4MissionItemInt.from_mapping(item) for item in WIRE_ITEMS))


def _ports() -> capture.CapturePorts:
    return capture.CapturePorts(51_001, 51_002, 51_003, 51_004, 51_005, 51_006)


def _set_nested(payload: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    changed = copy.deepcopy(payload)
    current: Any = changed
    parts = path.split(".")
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    if isinstance(current, list):
        current[int(parts[-1])] = value
    else:
        current[parts[-1]] = value
    return changed


def _flight_ready_payload(*, container_id: str = CONTAINER_ID) -> dict[str, Any]:
    return {
        "schema": "multidetect.px4-vtol-flight-capture-ready",
        "schema_version": 1,
        "captured_at_clock": "host_monotonic",
        "captured_at_monotonic_s": READY_MONOTONIC_S,
        "container": {"id": container_id, "exact_id_bound": True},
        "transaction": {
            "id": "tx-flight-capture-001",
            "state": "start_permitted",
            "start_permitted": True,
            "committed_protocol_sha256": PROTOCOL_HASH,
        },
        "flight": {
            "auto_mission_active": True,
            "auto_mission_custom_mode": PX4_AUTO_MISSION_CUSTOM_MODE,
            "armed": True,
            "in_air_at_ready": True,
            "fixed_wing_observed": True,
            "current_vtol_state": capture.MAV_VTOL_STATE_FW,
            "maximum_distance_from_home_m": capture.MINIMUM_FLIGHT_DISTANCE_M,
            "minimum_required_distance_from_home_m": capture.MINIMUM_FLIGHT_DISTANCE_M,
            "maximum_relative_altitude_m": capture.MINIMUM_FLIGHT_ALTITUDE_M,
            "minimum_required_relative_altitude_m": capture.MINIMUM_FLIGHT_ALTITUDE_M,
            "mission_progress": {
                "observed": True,
                "first_activity": {
                    "message_type": "MISSION_CURRENT",
                    "sequence": 1,
                    "at_s": 22.0,
                },
            },
            "position_at_ready": {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "relative_altitude_m": 10.0,
            },
            "telemetry_fresh": True,
            "maximum_telemetry_age_s": capture.MAXIMUM_FLIGHT_STATE_AGE_S,
            "telemetry_ages_s": {
                "heartbeat": 0.05,
                "position": 0.05,
                "extended_system_state": 0.05,
            },
        },
        "operator_visual_acceptance": "not_claimed",
        "real_pixhawk_contacted": False,
        "real_jetson_contacted": False,
        "real_camera_contacted": False,
    }


def _capture_complete_payload(*, container_id: str = CONTAINER_ID) -> dict[str, Any]:
    return {
        "schema": "multidetect.vtol-sensor-capture-complete",
        "schema_version": 1,
        "generated_at_utc": "2026-09-05T00:00:00Z",
        "clock": "host_monotonic",
        "capture_started_at_monotonic_s": READY_MONOTONIC_S + 0.1,
        "capture_completed_at_monotonic_s": READY_MONOTONIC_S + 0.2,
        "container_id": container_id,
        "transaction_id": "tx-flight-capture-001",
        "committed_protocol_sha256": PROTOCOL_HASH,
        "scenario_id": capture.EXPECTED_SCENARIO_ID,
        "scenario_manifest_hash": SCENARIO_HASH,
        "frame_count": 3,
        "sensor_binding_hash": "2" * 64,
        "frame_set_manifest_sha256": "3" * 64,
        "ground_truth_used_for_runtime_or_capture": False,
    }


def _write_completed_mission_journal(
    path: Path,
    *,
    mission_payload: dict[str, Any],
) -> dict[str, Any]:
    transaction = mission_payload["transaction"]
    protocol = mission_payload["protocol"]
    documents = mission_payload["documents"]
    executable_record = documents["executable_mission"]
    validation_record = documents["validation_report"]
    binding_values = documents["bindings"]
    transaction_id = transaction["id"]
    protocol_hash = protocol["committed_protocol_sha256"]
    mission_item_count = protocol["mission_item_count"]
    bindings = MissionTransactionBindings(
        prompt_hash=binding_values["prompt_hash"],
        task_spec_hash=binding_values["task_spec_hash"],
        plan_hash=binding_values["plan_hash"],
        context_snapshot_hash=binding_values["context_snapshot_hash"],
        airframe_profile_hash=binding_values["airframe_profile_hash"],
        geofence_hash=binding_values["geofence_hash"],
        vehicle_identity_hash=binding_values["vehicle_identity_hash"],
        runtime_descriptor_hash=binding_values["runtime_descriptor_hash"],
        runtime_provenance_hash=binding_values["runtime_provenance_hash"],
    )
    identity = MissionJournalIdentity(
        transaction_id=transaction_id,
        executable_mission_hash=executable_record["sha256"],
        protocol_hash=protocol_hash,
        validation_report_hash=validation_record["sha256"],
        bindings=bindings,
        mission_item_count=mission_item_count,
        airframe_adapter_id=executable_record["document"]["airframe_adapter_id"],
    )
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    journal = DurableMissionJournal.create(
        path,
        identity,
        journal_id="test-vtol-sensor-capture-journal",
        now_utc=now,
    )
    journal.record_transition(
        MissionTransactionState.UPLOADING,
        payload={"protocol_hash": protocol_hash, "item_count": mission_item_count},
        now_utc=now,
    )
    for sequence in range(mission_item_count):
        journal.record_item_request(sequence, now_utc=now)
    journal.record_transition(
        MissionTransactionState.UPLOADED,
        payload={
            "ack_type": 0,
            "mission_type": 0,
            "source_system": 1,
            "source_component": 1,
        },
        now_utc=now,
    )
    journal.record_transition(
        MissionTransactionState.READBACK_VERIFIED,
        payload={"protocol_hash": protocol_hash, "item_count": mission_item_count},
        now_utc=now,
    )
    journal.record_transition(
        MissionTransactionState.COMMITTED,
        payload={"protocol_hash": protocol_hash},
        now_utc=now,
    )
    journal.record_transition(
        MissionTransactionState.START_PERMITTED,
        payload={
            "transaction_id": transaction_id,
            "executable_mission_hash": identity.executable_mission_hash,
            "protocol_hash": protocol_hash,
        },
        now_utc=now,
    )
    claim = {
        "path": str(path.resolve()),
        "exists": True,
        "recovery_required": journal.recovery_required,
        "recovery_reason": journal.recovery_reason,
        "state": journal.state.value,
        "record_count": len(journal.records),
        "requested_sequences": list(journal.requested_sequences),
        "identity_hash": journal.identity.canonical_hash,
        "resume_plan": journal.resume_plan().to_mapping(),
    }
    journal.close()
    claim["file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return claim


def _mission_payload(
    *,
    container_id: str = CONTAINER_ID,
    airframe_hash: str = AIRFRAME_HASH,
    descriptor_hash: str = DESCRIPTOR_HASH,
    provenance_hash: str = PROVENANCE_HASH,
) -> dict[str, Any]:
    complete_document = _capture_complete_payload(container_id=container_id)
    transaction_id = "tx-flight-capture-001"

    def document_record(document: dict[str, Any]) -> dict[str, Any]:
        return {"document": document, "sha256": canonical_sha256(document)}

    prompt_record = document_record({"schema_version": 1, "prompt_id": "prompt-v56"})
    task_spec_record = document_record({"schema_version": 1, "task_id": "task-v56"})
    context_record = document_record({"schema_version": 1, "context_id": "context-v56"})
    plan_record = document_record({"schema_version": 1, "plan_id": "plan-v56"})
    executable_record = document_record(
        {
            "schema_version": 1,
            "mission_id": "mission-v56",
            "platform_plan_hash": plan_record["sha256"],
            "airframe_profile_hash": airframe_hash,
            "airframe_adapter_id": "px4-vtol-mission-int-v1",
            "items": [{"sequence": sequence} for sequence in range(3)],
        }
    )
    validation_record = document_record(
        {
            "schema_version": 1,
            "report_id": "validation-v56",
            "executable_mission_hash": executable_record["sha256"],
            "verdict": "pass",
        }
    )
    binding_values = {
        "prompt_hash": prompt_record["sha256"],
        "task_spec_hash": task_spec_record["sha256"],
        "plan_hash": plan_record["sha256"],
        "context_snapshot_hash": context_record["sha256"],
        "airframe_profile_hash": airframe_hash,
        "geofence_hash": "8" * 64,
        "vehicle_identity_hash": "9" * 64,
        "runtime_descriptor_hash": descriptor_hash,
        "runtime_provenance_hash": provenance_hash,
    }
    grant_record = document_record(
        {
            "schema_version": 1,
            "grant_id": "grant-v56",
            "transaction_id": transaction_id,
            "operator_id": "test-fixture",
            **binding_values,
            "executable_mission_hash": executable_record["sha256"],
            "validation_report_hash": validation_record["sha256"],
        }
    )
    documents = {
        "prompt": prompt_record,
        "task_spec": task_spec_record,
        "context_snapshot": context_record,
        "platform_neutral_plan": plan_record,
        "executable_mission": executable_record,
        "validation_report": validation_record,
        "operator_grant": grant_record,
        "bindings": binding_values,
    }
    return {
        "schema": "multidetect.px4-vtol-sitl-mission-acceptance",
        "schema_version": 1,
        "gate_passed": True,
        "container": {
            "id": container_id,
            "created_by_this_helper": False,
            "stopped_by_this_helper": False,
            "ownership_checks": {"exact_container_id": True, "running": True},
        },
        "transaction": {"id": transaction_id, "final_state": "start_permitted"},
        "protocol": {
            "upload_acknowledged": True,
            "readback_hash_match": True,
            "int_only": True,
            "mission_item_count": 3,
            "committed_protocol_sha256": PROTOCOL_HASH,
            "uploaded_protocol_sha256": PROTOCOL_HASH,
            "readback_protocol_sha256": PROTOCOL_HASH,
            "wire_items": [dict(item) for item in WIRE_ITEMS],
        },
        "documents": documents,
        "scenario": {
            "scenario_id": capture.EXPECTED_SCENARIO_ID,
            "manifest_sha256": SCENARIO_HASH,
            "truth_loaded_by_runtime": False,
            "expectations_loaded_by_runtime": False,
        },
        "mavlink": {
            "endpoint": "udpin:127.0.0.1:51005",
            "source_system": capture.EVALUATOR_SYSTEM_ID,
            "source_component": capture.EVALUATOR_COMPONENT_ID,
            "target_system": 1,
            "target_component": 1,
            "loopback_only": True,
            "protected_udp_14550_used": False,
        },
        "flight": {
            "fixed_wing_observed": True,
            "multicopter_observed": True,
            "landed_observed": True,
            "terminal_disarmed": True,
            "maximum_distance_from_home_m": capture.MINIMUM_FLIGHT_DISTANCE_M,
            "maximum_relative_altitude_m": capture.MINIMUM_FLIGHT_ALTITUDE_M,
            "flight_capture_ready_written": True,
            "flight_capture_ready_file": str(READY_PATH),
            "flight_capture_complete_observed": True,
            "flight_capture_complete_file": str(COMPLETE_PATH),
            "flight_capture_complete_file_sha256": COMPLETE_FILE_SHA256,
            "flight_capture_complete_document": complete_document,
            "flight_capture_complete_checks": {
                "schema": True,
                "schema_version": True,
                "host_monotonic_clock": True,
                "capture_interval": True,
                "exact_container_id": True,
                "transaction_id": True,
                "committed_protocol_sha256": True,
                "scenario_binding": True,
                "frame_set_binding": True,
                "truth_free": True,
            },
            "flight_capture_interval_state_source": "live MAVLink state observer",
            "flight_capture_interval_samples": [
                {
                    "captured_at_clock": "host_monotonic",
                    "captured_at_monotonic_s": READY_MONOTONIC_S + 0.1,
                    "auto_mission_active": True,
                    "armed": True,
                    "in_air": True,
                    "vtol_state": capture.MAV_VTOL_STATE_FW,
                    "maximum_state_age_s": capture.MAXIMUM_FLIGHT_STATE_AGE_S,
                    "heartbeat_age_s": 0.05,
                    "position_age_s": 0.05,
                    "extended_system_state_age_s": 0.05,
                    "telemetry_fresh": True,
                    "valid": True,
                },
                {
                    "captured_at_clock": "host_monotonic",
                    "captured_at_monotonic_s": READY_MONOTONIC_S + 0.2,
                    "auto_mission_active": True,
                    "armed": True,
                    "in_air": True,
                    "vtol_state": capture.MAV_VTOL_STATE_FW,
                    "maximum_state_age_s": capture.MAXIMUM_FLIGHT_STATE_AGE_S,
                    "heartbeat_age_s": 0.05,
                    "position_age_s": 0.05,
                    "extended_system_state_age_s": 0.05,
                    "telemetry_fresh": True,
                    "valid": True,
                },
            ],
            "flight_capture_interval_sample_count": 2,
            "flight_capture_interval_violation": False,
            "flight_capture_interval_qualified": True,
        },
        "safety": {
            "qgc_fly_view_visual_acceptance": "not_claimed",
            "real_pixhawk_contacted": False,
            "real_jetson_contacted": False,
            "real_camera_contacted": False,
            "hardware_control_enabled": False,
            "product_runtime_flight_control_enabled": False,
            "product_runtime_actuator_output_enabled": False,
        },
    }


def _sensor_payload(
    frame_directory: Path,
    *,
    frame_count: int = 3,
    width: int = 2,
    height: int = 2,
    scenario_id: str = capture.EXPECTED_SCENARIO_ID,
    scenario_manifest_hash: str = SCENARIO_HASH,
    live_timestamps: bool = False,
    clock=None,
) -> dict[str, Any]:
    frame_directory.mkdir(parents=True, exist_ok=False)
    payload_bytes = width * height * 3
    sensor_binding_hash = "2" * 64
    sensor_frames: list[dict[str, Any]] = []
    persisted_frames: list[dict[str, Any]] = []
    last_captured_at_s: float | None = None
    for index in range(frame_count):
        raw = bytes([index + 1]) * payload_bytes
        payload_hash = hashlib.sha256(raw).hexdigest()
        record_hash = f"{index + 3:x}" * 64
        frame_id = f"gazebo-sim-{index:06d}"
        frame_name = f"frame-{index:06d}.bgr24"
        (frame_directory / frame_name).write_bytes(raw)
        if live_timestamps:
            captured_at_s = clock.monotonic() if clock is not None else capture.time.monotonic()
            while last_captured_at_s is not None and captured_at_s <= last_captured_at_s:
                captured_at_s = clock.monotonic() if clock is not None else capture.time.monotonic()
        else:
            captured_at_s = READY_MONOTONIC_S + 0.1 + (0.1 * index / max(frame_count - 1, 1))
        last_captured_at_s = captured_at_s
        provenance = {
            "captured_at_clock": "host_perf_counter" if clock is not None else "host_monotonic",
            **({"host_clock_identity": clock.identity()} if clock is not None else {}),
            "captured_at_s": captured_at_s,
            "sensor_stamp_ns": 1_000_000_000 + index,
            "sequence": index,
            "frame_id": frame_id,
            "width": width,
            "height": height,
            "stride": width * 3,
            "pixel_format": "BGR24",
            "payload_bytes": payload_bytes,
            "payload_sha256": payload_hash,
            "record_sha256": record_hash,
        }
        persisted_frames.append(
            {
                "frame_id": frame_id,
                "sequence": index,
                "file": frame_name,
                "bytes": payload_bytes,
                "sha256": payload_hash,
                "wire_payload_sha256": payload_hash,
                "raw_bytes_sha256_matches_wire_payload": True,
                "wire_record_sha256": record_hash,
                "provenance": provenance,
            }
        )
        sensor_frames.append(
            {
                **provenance,
                "bgr_shape": [height, width, 3],
                "persisted": True,
                "persisted_file": frame_name,
                "persisted_bytes": payload_bytes,
                "persisted_sha256": payload_hash,
                "raw_bytes_sha256_matches_wire_payload": True,
            }
        )
    manifest_without_hash: dict[str, Any] = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 1,
        "generated_at_utc": "2026-09-05T00:00:00Z",
        "scenario_id": scenario_id,
        "scenario_manifest_hash": scenario_manifest_hash,
        "sensor_binding_hash": sensor_binding_hash,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "frame_count": frame_count,
        "total_payload_bytes": frame_count * payload_bytes,
        "frames": persisted_frames,
        "source_attribution": {
            "pixels": "actual_gazebo_camera_sensor_output",
            "environment": "software_simulation",
            "wire": "MDGZSIM3",
            "source_row_order": capture.GAZEBO_SIM_SOURCE_ROW_ORDER,
            "wire_row_order": capture.GAZEBO_SIM_WIRE_ROW_ORDER,
            "vertical_flip_applied": capture.GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
            "truth_loaded_by_runtime": False,
        },
        "claims": {
            "actual_gazebo_pixels_persisted": True,
            "qgc_rendering": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "hardware": "not_claimed",
            "detector_evaluation": "not_claimed",
            "production_model_approval": "not_claimed",
        },
    }
    if clock is not None:
        manifest_without_hash["captured_at_clock"] = "host_perf_counter"
        manifest_without_hash["host_clock_identity"] = clock.identity()
    manifest_hash = canonical_sha256(manifest_without_hash)
    manifest_payload = {**manifest_without_hash, "manifest_sha256": manifest_hash}
    manifest_path = frame_directory / capture.GAZEBO_BGR24_FRAME_SET_MANIFEST
    manifest_path.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "qualified": True,
        "wire_version": 3,
        "source_row_order": capture.GAZEBO_SIM_SOURCE_ROW_ORDER,
        "wire_row_order": capture.GAZEBO_SIM_WIRE_ROW_ORDER,
        "vertical_flip_applied": capture.GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
        "truth_free": True,
        "full_odometry_pose_velocity_fusion_claimed": True,
        "requested_frame_count": frame_count,
        "sensor_binding_hash": sensor_binding_hash,
        "frames": sensor_frames,
        "transport": {
            "complete": True,
            "helper_exit_code": 0,
            "expected_frame_count": frame_count,
            "received_frame_count": frame_count,
            "sensor_binding_sha256": sensor_binding_hash,
            "source_row_order": capture.GAZEBO_SIM_SOURCE_ROW_ORDER,
            "wire_row_order": capture.GAZEBO_SIM_WIRE_ROW_ORDER,
            "vertical_flip_applied": capture.GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
        },
        "pixel_persistence": {
            "requested": True,
            "status": "passed",
            "output_directory": str(frame_directory),
            "persisted_frame_count": frame_count,
            "total_payload_bytes": frame_count * payload_bytes,
            "actual_gazebo_pixels_persisted": True,
            "raw_bytes_sha256_matches_wire_payload": True,
            "files": persisted_frames,
            "manifest": {
                "path": str(manifest_path),
                "file": manifest_path.name,
                "bytes": manifest_path.stat().st_size,
                "file_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "manifest_sha256": manifest_hash,
            },
            "qgc_rendering": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "hardware": "not_claimed",
        },
    }


def _validate_sensor(
    payload: dict[str, Any],
    frame_directory: Path,
    *,
    frame_count: int = 3,
    width: int = 2,
    height: int = 2,
    host_clock_identity=None,
) -> dict[str, bool]:
    return capture.validate_sensor_capture(
        payload,
        frame_count=frame_count,
        frame_directory=frame_directory,
        scenario_id=capture.EXPECTED_SCENARIO_ID,
        scenario_manifest_hash=SCENARIO_HASH,
        expected_width=width,
        expected_height=height,
        flight_ready_monotonic_s=READY_MONOTONIC_S,
        capture_started_monotonic_s=READY_MONOTONIC_S + 0.1,
        capture_completed_monotonic_s=READY_MONOTONIC_S + 0.2,
        host_clock_identity=host_clock_identity,
    )


def _router_payload() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "event": "px4_gazebo_qgc_router_finished",
        "termination_reason": "stop_event_set",
        "ready_barrier": {
            "reached": True,
            "all_udp_sockets_bound_before_ready": True,
            "expected_udp_socket_count": 3,
            "bound_udp_socket_count": 3,
            "ready_event_supplied": True,
            "ready_event_set": True,
            "stop_event_supplied": True,
        },
        "endpoints": {
            "px4_telemetry_input": "127.0.0.1:51002",
            "px4_gcs_input": "127.0.0.1:51001",
            "qgc": "127.0.0.1:51003",
            "qgc_router_return": "127.0.0.1:51004",
            "evaluator": "127.0.0.1:51005",
            "evaluator_router_return": "127.0.0.1:51006",
        },
        "evaluator_identity": {
            "system_id": capture.EVALUATOR_SYSTEM_ID,
            "component_id": capture.EVALUATOR_COMPONENT_ID,
        },
        "evaluator_identity_enforced": True,
        "px4_frames_forwarded_to_evaluator": 5,
        "evaluator_mission_frames_forwarded": 4,
        "evaluator_control_commands_forwarded": 2,
        "px4_discarded_bytes": 0,
        "qgc_discarded_bytes": 0,
        "evaluator_discarded_bytes": 0,
        "all_udp_endpoints_loopback": True,
        "all_ports_distinct": True,
        "protected_udp_14550_used": False,
        "acknowledge_owned_disposable_sitl": True,
        "evaluator_bounded_mission_transaction_allowed": True,
        "evaluator_mission_clear_all_allowed": False,
        "evaluator_set_mode_restricted_to_px4_auto_mission": True,
        "evaluator_arbitrary_commands_allowed": False,
        "qgc_parameter_writes_allowed": False,
        "qgc_flight_commands_allowed": False,
        "qgc_mission_writes_allowed": False,
        "software_only": True,
        "real_pixhawk_contacted": False,
        "physical_actuator_outputs_present": False,
        "payload_commands_allowed": False,
    }


def test_capture_ports_mapping_is_six_distinct_high_loopback_endpoints() -> None:
    ports = capture.validate_capture_ports(_ports(), port_probe=lambda _port: True)
    mapping = ports.to_mapping()

    assert ports.values == (51_001, 51_002, 51_003, 51_004, 51_005, 51_006)
    assert mapping["host"] == "127.0.0.1"
    assert mapping["all_distinct"] is True
    assert mapping["all_high_ports"] is True
    assert mapping["protected_udp_14550_used"] is False
    assert mapping["qgc_process_started"] is False


@pytest.mark.parametrize(
    ("ports", "message"),
    (
        (capture.CapturePorts(True, 51_002, 51_003, 51_004, 51_005, 51_006), "integers"),
        (capture.CapturePorts(51_001, 51_001, 51_003, 51_004, 51_005, 51_006), "distinct"),
        (
            capture.CapturePorts(14_550, 51_002, 51_003, 51_004, 51_005, 51_006),
            "14550",
        ),
        (capture.CapturePorts(29_999, 51_002, 51_003, 51_004, 51_005, 51_006), "30000"),
        (capture.CapturePorts(60_001, 51_002, 51_003, 51_004, 51_005, 51_006), "60000"),
    ),
)
def test_capture_ports_reject_invalid_types_duplicates_protected_and_bounds(
    ports: capture.CapturePorts,
    message: str,
) -> None:
    with pytest.raises(capture.VtolMissionSensorCaptureError, match=message):
        capture.validate_capture_ports(ports, port_probe=lambda _port: True)


def test_capture_ports_reject_every_occupied_endpoint_in_one_fail_closed_error() -> None:
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="51002, 51005"):
        capture.validate_capture_ports(
            _ports(),
            port_probe=lambda port: port not in {51_002, 51_005},
        )


class _SequenceRandom:
    def __init__(self, values: list[int]) -> None:
        self._values = iter(values)

    def randint(self, _minimum: int, _maximum: int) -> int:
        return next(self._values)


def test_allocator_skips_duplicate_and_occupied_candidates() -> None:
    values = [51_001, 51_001, 51_002, 51_003, 51_004, 51_005, 51_006, 51_007]
    ports = capture.allocate_capture_ports(
        random_source=_SequenceRandom(values),  # type: ignore[arg-type]
        port_probe=lambda port: port != 51_002,
        maximum_attempts=len(values),
    )

    assert ports.values == (51_001, 51_003, 51_004, 51_005, 51_006, 51_007)


def test_allocator_fails_when_six_free_ports_cannot_be_found() -> None:
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="unable to allocate"):
        capture.allocate_capture_ports(
            random_source=_SequenceRandom([51_001] * 6),  # type: ignore[arg-type]
            port_probe=lambda _port: True,
            maximum_attempts=6,
        )


def test_capture_paths_are_absolute_distinct_and_no_clobber(tmp_path: Path) -> None:
    paths = capture.prepare_capture_paths(tmp_path / "run", tmp_path / "evidence.json")

    assert paths.run_directory.is_dir()
    assert all(Path(value).is_absolute() for value in paths.to_mapping().values())
    assert len(set(value for value in paths.to_mapping().values() if value is not None)) == 10
    assert paths.depth_directory is None
    assert paths.flight_capture_complete.name == "vtol-sensor-capture-complete.json"
    assert not paths.evidence.exists()

    existing = tmp_path / "existing-evidence.json"
    existing.write_bytes(b"preserve-me")
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="new path"):
        capture.prepare_capture_paths(tmp_path / "second-run", existing)
    assert existing.read_bytes() == b"preserve-me"
    assert not (tmp_path / "second-run").exists()


def test_capture_paths_include_explicit_depth_directory_without_implicit_capture(
    tmp_path: Path,
) -> None:
    paths = capture.prepare_capture_paths(
        tmp_path / "depth-run",
        tmp_path / "depth-evidence.json",
        depth_output_directory=tmp_path / "depth-frames",
    )

    assert paths.depth_directory == (tmp_path / "depth-frames").resolve()
    mapping = paths.to_mapping()
    assert mapping["depth_directory"] == str(paths.depth_directory)
    assert len(set(mapping.values())) == 11


def test_capture_paths_reject_aliases_before_creating_run(tmp_path: Path) -> None:
    aliased = tmp_path / "same"
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="pairwise distinct"):
        capture.prepare_capture_paths(
            aliased,
            tmp_path / "evidence.json",
            frame_output_directory=aliased,
        )
    assert not aliased.exists()


def test_capture_paths_reject_ancestor_descendant_output_aliases(tmp_path: Path) -> None:
    output_parent = tmp_path / "nested-output"
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="contain one another"):
        capture.prepare_capture_paths(
            tmp_path / "run",
            tmp_path / "evidence.json",
            flight_ready_file=output_parent,
            frame_output_directory=output_parent / "frames",
        )
    assert not (tmp_path / "run").exists()
    assert not output_parent.exists()


def test_atomic_json_write_refuses_to_replace_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "evidence.json"
    capture._atomic_write_new_json(target, {"first": True})
    original = target.read_bytes()

    with pytest.raises(FileExistsError):
        capture._atomic_write_new_json(target, {"second": True})
    assert target.read_bytes() == original


def test_atomic_json_publish_has_one_winner_under_a_writer_race(tmp_path: Path) -> None:
    target = tmp_path / "race-evidence.json"
    barrier = threading.Barrier(2)

    def writer(writer_id: int) -> tuple[str, int]:
        barrier.wait(timeout=2.0)
        try:
            capture._atomic_write_new_json(target, {"writer_id": writer_id})
        except (FileExistsError, capture.VtolMissionSensorCaptureError):
            return "rejected", writer_id
        return "published", writer_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(writer, (1, 2)))

    assert sorted(status for status, _writer_id in outcomes) == ["published", "rejected"]
    winning_id = next(writer_id for status, writer_id in outcomes if status == "published")
    assert json.loads(target.read_text(encoding="utf-8")) == {"writer_id": winning_id}
    assert not list(tmp_path.glob(f".{target.name}.*.tmp"))


def test_atomic_json_publish_cleans_temporary_file_when_link_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "publish-failure.json"

    def fail_link(_source: Path, _target: Path) -> None:
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(capture.os, "link", fail_link)
    with pytest.raises(OSError, match="synthetic publish failure"):
        capture._atomic_write_new_json(target, {"must_not_publish": True})

    assert not target.exists()
    assert not list(tmp_path.glob(f".{target.name}.*.tmp"))


def test_hardened_docker_plan_contract_and_weakened_argv_rejection(tmp_path: Path) -> None:
    ports = _ports()
    plan = build_docker_simulator_plan(
        SCENARIO,
        repository_root=ROOT,
        job_dir=tmp_path / "plan",
        container_name="multidetect-vtol-capture-test",
        control_port=ports.px4_input,
        telemetry_port=ports.px4_telemetry,
    )

    checks = capture.validate_capture_plan(
        plan,
        ports=ports,
        container_name="multidetect-vtol-capture-test",
    )
    assert all(checks.values())
    assert checks["cap_drop_all"] is True
    assert checks["no_new_privileges"] is True
    assert checks["container_security_gate"] is True
    assert checks["protected_udp_14550_unused"] is True

    cap_index = plan.command.index("--cap-drop")
    weakened = replace(plan, command=plan.command[:cap_index] + plan.command[cap_index + 2 :])
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="cap_drop_all"):
        capture.validate_capture_plan(
            weakened,
            ports=ports,
            container_name="multidetect-vtol-capture-test",
        )


@pytest.mark.parametrize("qpc", [False, True])
def test_flight_ready_accepts_only_strict_mission_flight_barrier(qpc) -> None:
    from multidetect.clock import HighResolutionSystemClock

    payload = _flight_ready_payload()
    identity = HighResolutionSystemClock(session_id="barrier-parent").identity() if qpc else None
    if qpc:
        payload["captured_at_clock"] = "host_perf_counter"
        payload["host_clock_identity"] = identity
    checks = capture.validate_flight_capture_ready(
        payload,
        container_id=CONTAINER_ID,
        host_clock_identity=identity,
    )
    assert all(checks.values())
    if qpc:
        with pytest.raises(capture.VtolMissionSensorCaptureError):
            capture.validate_flight_capture_ready(payload, container_id=CONTAINER_ID)


def test_campaign_flight_ready_binds_task_descriptor_and_rejects_tampering() -> None:
    expected = {
        "task_id": "task-v56",
        "plan_sha256": "a" * 64,
        "executable_mission_sha256": "b" * 64,
        "wire_hash": "c" * 64,
    }
    payload = _flight_ready_payload()
    payload["task_binding"] = dict(expected)
    checks = capture.validate_flight_capture_ready(
        payload,
        container_id=CONTAINER_ID,
        expected_task_binding=expected,
    )
    assert checks["task_binding"] is True

    for field in expected:
        tampered = copy.deepcopy(payload)
        tampered["task_binding"][field] = "tampered"
        with pytest.raises(capture.VtolMissionSensorCaptureError, match="task_binding"):
            capture.validate_flight_capture_ready(
                tampered,
                container_id=CONTAINER_ID,
                expected_task_binding=expected,
            )

    missing = copy.deepcopy(payload)
    del missing["task_binding"]
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="task_binding"):
        capture.validate_flight_capture_ready(
            missing,
            container_id=CONTAINER_ID,
            expected_task_binding=expected,
        )


@pytest.mark.parametrize(
    ("path", "value", "message"),
    (
        ("schema_version", True, "schema_version"),
        ("container.id", "e" * 64, "exact_container_id"),
        ("container.exact_id_bound", False, "exact_container_id_bound"),
        ("captured_at_monotonic_s", float("inf"), "host_monotonic_timestamp"),
        ("transaction.state", "uploading", "transaction_start_permitted"),
        ("transaction.id", "   ", "transaction_id"),
        ("transaction.start_permitted", False, "transaction_start_permitted"),
        ("transaction.committed_protocol_sha256", "short", "committed_protocol_sha256"),
        ("flight.auto_mission_active", False, "exact_auto_mission"),
        ("flight.auto_mission_custom_mode", 0, "exact_auto_mission"),
        ("flight.armed", False, "armed"),
        ("flight.in_air_at_ready", False, "in_air"),
        ("flight.fixed_wing_observed", False, "fixed_wing"),
        ("flight.current_vtol_state", 3, "fixed_wing"),
        ("flight.maximum_distance_from_home_m", 34.999, "distance"),
        ("flight.maximum_distance_from_home_m", float("inf"), "distance"),
        ("flight.maximum_relative_altitude_m", 9.999, "altitude"),
        ("flight.maximum_relative_altitude_m", float("nan"), "altitude"),
        ("flight.mission_progress.observed", False, "mission_progress"),
        ("flight.position_at_ready", None, "position_at_ready"),
        ("flight.position_at_ready", {}, "position_at_ready"),
        ("flight.position_at_ready.latitude_deg", 90.0, "position_at_ready"),
        ("flight.position_at_ready.longitude_deg", 180.001, "position_at_ready"),
        ("flight.position_at_ready.relative_altitude_m", float("nan"), "position_at_ready"),
        ("flight.telemetry_fresh", False, "telemetry_fresh"),
        ("flight.maximum_telemetry_age_s", 1.4, "telemetry_fresh"),
        ("flight.telemetry_ages_s.heartbeat", 1.501, "telemetry_fresh"),
        ("flight.telemetry_ages_s.position", -0.001, "telemetry_fresh"),
        ("flight.telemetry_ages_s.extended_system_state", None, "telemetry_fresh"),
        ("operator_visual_acceptance", "passed", "no_visual_claim"),
        ("real_camera_contacted", True, "no_real_hardware"),
    ),
)
@pytest.mark.parametrize("qpc", [False, True])
def test_flight_ready_fails_closed_when_any_safety_fact_is_missing(
    path: str,
    value: Any,
    message: str,
    qpc: bool,
) -> None:
    from multidetect.clock import HighResolutionSystemClock

    payload = _set_nested(_flight_ready_payload(), path, value)
    identity = HighResolutionSystemClock(session_id="barrier-parent").identity() if qpc else None
    if qpc:
        payload["captured_at_clock"] = "host_perf_counter"
        payload["host_clock_identity"] = identity
    with pytest.raises(capture.VtolMissionSensorCaptureError, match=message):
        capture.validate_flight_capture_ready(
            payload, container_id=CONTAINER_ID, host_clock_identity=identity
        )


@pytest.mark.parametrize("change", ["missing", "null", "session", "label", "resolution"])
def test_flight_ready_rejects_clock_identity_tampering(change):
    from multidetect.clock import HighResolutionSystemClock

    identity = HighResolutionSystemClock(session_id="barrier-parent").identity()
    payload = _flight_ready_payload()
    payload["captured_at_clock"] = "host_perf_counter"
    payload["host_clock_identity"] = dict(identity)
    if change == "missing":
        del payload["host_clock_identity"]
    elif change == "null":
        payload["host_clock_identity"] = None
    elif change == "session":
        payload["host_clock_identity"]["session_id"] = "other"
    elif change == "resolution":
        payload["host_clock_identity"]["resolution_ns"] += 1
    else:
        payload["captured_at_clock"] = "host_monotonic"
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="host_monotonic_timestamp"):
        capture.validate_flight_capture_ready(
            payload, container_id=CONTAINER_ID, host_clock_identity=identity
        )


@pytest.mark.parametrize("qpc", [False, True])
@pytest.mark.parametrize("tamper", [None, "ready", "complete", "interval"])
def test_mission_evidence_requires_completed_vtol_and_same_committed_protocol(qpc, tamper) -> None:
    from multidetect.clock import HighResolutionSystemClock

    identity = HighResolutionSystemClock(session_id="capture-parent").identity() if qpc else None
    payload = _mission_payload()
    ready = _flight_ready_payload()
    complete = _capture_complete_payload()
    samples = payload["flight"]["flight_capture_interval_samples"]
    records = [(ready, "captured_at_clock"), (complete, "clock")]
    records.extend((sample, "captured_at_clock") for sample in samples)
    if qpc:
        for record, label_key in records:
            record[label_key] = "host_perf_counter"
            record["host_clock_identity"] = dict(identity)
    if tamper:
        record = {"ready": ready, "complete": complete, "interval": samples[0]}[tamper]
        record["host_clock_identity"] = {"source": "python.perf_counter", "session_id": "wrong"}
    payload["flight"]["flight_capture_complete_document"] = complete
    kwargs = dict(
        container_id=CONTAINER_ID,
        scenario_manifest_sha256=SCENARIO_HASH,
        airframe_profile_hash=AIRFRAME_HASH,
        descriptor_hash=DESCRIPTOR_HASH,
        provenance_hash=PROVENANCE_HASH,
        evaluator_port=51_005,
        barrier=ready,
        flight_ready_path=READY_PATH,
        flight_complete_path=COMPLETE_PATH,
        flight_complete_file_sha256=COMPLETE_FILE_SHA256,
        flight_complete_document=complete,
        host_clock_identity=identity,
    )
    if tamper:
        with pytest.raises(
            capture.VtolMissionSensorCaptureError,
            match="capture_clock_domain|flight_capture_interval",
        ):
            capture.validate_mission_evidence(payload, **kwargs)
        return
    checks = capture.validate_mission_evidence(payload, **kwargs)
    assert all(checks.values())


def test_mission_evidence_binds_ready_task_descriptor_to_completion() -> None:
    payload = _mission_payload()
    ready = _flight_ready_payload()
    complete = _capture_complete_payload()
    expected = {
        "task_id": "task-v56",
        "plan_sha256": payload["documents"]["platform_neutral_plan"]["sha256"],
        "executable_mission_sha256": payload["documents"]["executable_mission"]["sha256"],
        "wire_hash": PROTOCOL_HASH,
    }
    ready["task_binding"] = dict(expected)
    complete.update(expected)
    complete["capture_deadline_monotonic_s"] = READY_MONOTONIC_S + 1.0
    payload["flight"]["flight_capture_complete_document"] = complete
    payload["flight"]["flight_capture_complete_checks"].update(
        task_binding=True,
        capture_deadline=True,
    )
    kwargs = dict(
        container_id=CONTAINER_ID,
        scenario_manifest_sha256=SCENARIO_HASH,
        airframe_profile_hash=AIRFRAME_HASH,
        descriptor_hash=DESCRIPTOR_HASH,
        provenance_hash=PROVENANCE_HASH,
        evaluator_port=51_005,
        barrier=ready,
        flight_ready_path=READY_PATH,
        flight_complete_path=COMPLETE_PATH,
        flight_complete_file_sha256=COMPLETE_FILE_SHA256,
        flight_complete_document=complete,
        expected_task_binding=expected,
    )
    checks = capture.validate_mission_evidence(payload, **kwargs)
    assert checks["task_binding"] is True

    tampered = copy.deepcopy(ready)
    tampered["task_binding"]["wire_hash"] = "d" * 64
    with pytest.raises(capture.VtolMissionSensorCaptureError, match="task_binding"):
        capture.validate_mission_evidence(payload, **{**kwargs, "barrier": tampered})


def test_mission_journal_reopens_and_binds_all_mission_identity_fields(
    tmp_path: Path,
) -> None:
    payload = _mission_payload()
    journal_path = tmp_path / "mission-journal.jsonl"
    payload["mission_journal"] = _write_completed_mission_journal(
        journal_path,
        mission_payload=payload,
    )

    checks = capture.validate_mission_journal(
        journal_path,
        payload,
        transaction_id=payload["transaction"]["id"],
        committed_protocol_sha256=PROTOCOL_HASH,
    )

    assert all(checks.values())
    assert checks["canonical_document_hashes"] is True
    assert checks["identity_bindings"] is True
    assert checks["executable_mission_identity"] is True
    assert checks["validation_report_identity"] is True
    assert checks["operator_grant_identity"] is True
    assert checks["mission_item_count"] is True
    assert checks["serialized_wire_items"] is True
    assert checks["airframe_adapter_id"] is True


@pytest.mark.parametrize(
    ("path", "value", "record_to_rehash", "message"),
    (
        (
            "documents.bindings.runtime_descriptor_hash",
            "0" * 64,
            None,
            "identity_bindings",
        ),
        (
            "documents.bindings.runtime_provenance_hash",
            "0" * 64,
            None,
            "identity_bindings",
        ),
        (
            "documents.executable_mission.document.mission_id",
            "tampered-mission",
            "executable_mission",
            "executable_mission_identity",
        ),
        (
            "documents.validation_report.document.verdict",
            "tampered",
            "validation_report",
            "validation_report_identity",
        ),
        (
            "protocol.mission_item_count",
            4,
            None,
            "mission_item_count",
        ),
        (
            "documents.executable_mission.document.airframe_adapter_id",
            "tampered-adapter",
            "executable_mission",
            "airframe_adapter_id",
        ),
        (
            "protocol.wire_items.1.param1",
            1.0,
            None,
            "serialized_wire_items",
        ),
        (
            "protocol.wire_items",
            [],
            None,
            "serialized_wire_items",
        ),
    ),
)
def test_mission_journal_rejects_identity_or_wire_item_tampering(
    tmp_path: Path,
    path: str,
    value: Any,
    record_to_rehash: str | None,
    message: str,
) -> None:
    payload = _mission_payload()
    journal_path = tmp_path / "mission-journal.jsonl"
    payload["mission_journal"] = _write_completed_mission_journal(
        journal_path,
        mission_payload=payload,
    )
    tampered = _set_nested(payload, path, value)
    if record_to_rehash is not None:
        record = tampered["documents"][record_to_rehash]
        record["sha256"] = canonical_sha256(record["document"])

    with pytest.raises(capture.VtolMissionSensorCaptureError, match=message):
        capture.validate_mission_journal(
            journal_path,
            tampered,
            transaction_id=payload["transaction"]["id"],
            committed_protocol_sha256=PROTOCOL_HASH,
        )


@pytest.mark.parametrize(
    ("argument", "value"),
    (
        ("flight_ready_path", COMPLETE_PATH),
        ("flight_complete_path", READY_PATH),
        ("flight_complete_file_sha256", "0" * 64),
        ("flight_complete_document", {"tampered": True}),
    ),
)
def test_mission_evidence_rejects_unbound_outer_handshake_inputs(
    argument: str,
    value: Any,
) -> None:
    handshake: dict[str, Any] = {
        "flight_ready_path": READY_PATH,
        "flight_complete_path": COMPLETE_PATH,
        "flight_complete_file_sha256": COMPLETE_FILE_SHA256,
        "flight_complete_document": _capture_complete_payload(),
    }
    handshake[argument] = value

    with pytest.raises(capture.VtolMissionSensorCaptureError, match="flight_capture"):
        capture.validate_mission_evidence(
            _mission_payload(),
            container_id=CONTAINER_ID,
            scenario_manifest_sha256=SCENARIO_HASH,
            airframe_profile_hash=AIRFRAME_HASH,
            descriptor_hash=DESCRIPTOR_HASH,
            provenance_hash=PROVENANCE_HASH,
            evaluator_port=51_005,
            barrier=_flight_ready_payload(),
            **handshake,
        )


@pytest.mark.parametrize(
    ("path", "value", "message"),
    (
        ("gate_passed", False, "gate_passed"),
        ("container.id", "e" * 64, "exact_container_id"),
        ("container.ownership_checks.exact_container_id", False, "container_remained_parent_owned"),
        ("transaction.final_state", "failed", "transaction_start_permitted"),
        ("protocol.readback_hash_match", False, "protocol_committed"),
        ("protocol.uploaded_protocol_sha256", "f" * 64, "protocol_committed"),
        ("scenario.manifest_sha256", "f" * 64, "scenario_bound"),
        ("documents.bindings.airframe_profile_hash", "f" * 64, "airframe_profile_bound"),
        ("documents.bindings.runtime_descriptor_hash", "f" * 64, "runtime_descriptor_bound"),
        ("documents.bindings.runtime_provenance_hash", "f" * 64, "runtime_provenance_bound"),
        ("mavlink.endpoint", "udpin:127.0.0.1:51006", "evaluator_endpoint_bound"),
        ("flight.fixed_wing_observed", False, "fixed_wing"),
        ("flight.multicopter_observed", False, "multicopter_after_fixed_wing"),
        ("flight.landed_observed", False, "vtol_landed"),
        ("flight.terminal_disarmed", False, "terminal_disarmed"),
        ("flight.maximum_distance_from_home_m", 34.999, "distance"),
        ("flight.maximum_distance_from_home_m", float("inf"), "distance"),
        ("flight.maximum_relative_altitude_m", 9.999, "altitude"),
        ("flight.flight_capture_ready_written", False, "flight_capture"),
        ("flight.flight_capture_ready_file", str(COMPLETE_PATH), "flight_capture"),
        ("flight.flight_capture_complete_observed", False, "flight_capture"),
        ("flight.flight_capture_complete_file", str(READY_PATH), "flight_capture"),
        ("flight.flight_capture_complete_file_sha256", "0" * 64, "flight_capture"),
        ("flight.flight_capture_complete_document", {"tampered": True}, "flight_capture"),
        ("flight.flight_capture_complete_checks.schema", False, "flight_capture"),
        ("flight.flight_capture_interval_state_source", "claimed", "flight_capture"),
        ("flight.flight_capture_interval_samples", [], "flight_capture"),
        ("flight.flight_capture_interval_sample_count", 0, "flight_capture"),
        (
            "flight.flight_capture_interval_samples.0.auto_mission_active",
            False,
            "flight_capture",
        ),
        ("flight.flight_capture_interval_samples.0.armed", False, "flight_capture"),
        ("flight.flight_capture_interval_samples.0.in_air", False, "flight_capture"),
        (
            "flight.flight_capture_interval_samples.0.vtol_state",
            3,
            "flight_capture",
        ),
        (
            "flight.flight_capture_interval_samples.0.maximum_state_age_s",
            1.4,
            "flight_capture",
        ),
        (
            "flight.flight_capture_interval_samples.0.heartbeat_age_s",
            1.501,
            "flight_capture",
        ),
        (
            "flight.flight_capture_interval_samples.0.position_age_s",
            -0.001,
            "flight_capture",
        ),
        (
            "flight.flight_capture_interval_samples.0.extended_system_state_age_s",
            None,
            "flight_capture",
        ),
        (
            "flight.flight_capture_interval_samples.0.telemetry_fresh",
            False,
            "flight_capture",
        ),
        (
            "flight.flight_capture_interval_samples.0.captured_at_monotonic_s",
            READY_MONOTONIC_S + 0.101,
            "flight_capture",
        ),
        (
            "flight.flight_capture_interval_samples.1.captured_at_monotonic_s",
            READY_MONOTONIC_S + 0.199,
            "flight_capture",
        ),
        ("flight.flight_capture_interval_samples.0.valid", False, "flight_capture"),
        ("flight.flight_capture_interval_violation", True, "flight_capture"),
        ("flight.flight_capture_interval_qualified", False, "flight_capture"),
        ("safety.qgc_fly_view_visual_acceptance", "passed", "no_qgc_visual_claim"),
        ("safety.real_pixhawk_contacted", True, "no_real_hardware"),
    ),
)
def test_mission_evidence_fails_closed_on_incomplete_or_overclaimed_run(
    path: str,
    value: Any,
    message: str,
) -> None:
    payload = _set_nested(_mission_payload(), path, value)
    with pytest.raises(capture.VtolMissionSensorCaptureError, match=message):
        capture.validate_mission_evidence(
            payload,
            container_id=CONTAINER_ID,
            scenario_manifest_sha256=SCENARIO_HASH,
            airframe_profile_hash=AIRFRAME_HASH,
            descriptor_hash=DESCRIPTOR_HASH,
            provenance_hash=PROVENANCE_HASH,
            evaluator_port=51_005,
            barrier=_flight_ready_payload(),
            flight_ready_path=READY_PATH,
            flight_complete_path=COMPLETE_PATH,
            flight_complete_file_sha256=COMPLETE_FILE_SHA256,
            flight_complete_document=_capture_complete_payload(),
        )


@pytest.mark.parametrize("clock_case", ["legacy", "qpc", "wrong-parent", "missing-parent"])
def test_wire_v3_capture_requires_persisted_truth_free_actual_pixels(
    tmp_path: Path, clock_case
) -> None:
    from multidetect.clock import HighResolutionSystemClock

    frame_directory = tmp_path / "frames"
    payload = _sensor_payload(frame_directory)
    identity = None
    if clock_case != "legacy":
        identity = HighResolutionSystemClock(session_id="parent-pixels").identity()
        manifest_record = payload["pixel_persistence"]["manifest"]
        manifest_path = Path(manifest_record["path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["captured_at_clock"] = "host_perf_counter"
        manifest["host_clock_identity"] = dict(identity)
        for frame in payload["pixel_persistence"]["files"]:
            frame["provenance"]["captured_at_clock"] = "host_perf_counter"
            frame["provenance"]["host_clock_identity"] = dict(identity)
        manifest["frames"] = payload["pixel_persistence"]["files"]
        manifest.pop("manifest_sha256")
        manifest["manifest_sha256"] = canonical_sha256(manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_record.update(
            bytes=manifest_path.stat().st_size,
            file_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            manifest_sha256=manifest["manifest_sha256"],
        )
        if clock_case == "wrong-parent":
            identity = HighResolutionSystemClock(session_id="different-parent").identity()
        elif clock_case == "missing-parent":
            identity = None
    if clock_case in ("wrong-parent", "missing-parent"):
        with pytest.raises(capture.VtolMissionSensorCaptureError):
            _validate_sensor(payload, frame_directory, host_clock_identity=identity)
        return
    checks = _validate_sensor(payload, frame_directory, host_clock_identity=identity)
    assert all(checks.values())


@pytest.mark.parametrize("version", (1, 2, 3, True, 2.0))
def test_pixel_gate_accepts_known_manifest_versions_only(tmp_path: Path, version) -> None:
    frame_directory = tmp_path / "frames"
    payload = _sensor_payload(frame_directory)
    manifest_path = Path(payload["pixel_persistence"]["manifest"]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = version
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256")
    manifest["manifest_sha256"] = canonical_sha256(unsigned)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    record = payload["pixel_persistence"]["manifest"]
    record.update(
        {
            "bytes": manifest_path.stat().st_size,
            "file_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "manifest_sha256": manifest["manifest_sha256"],
        }
    )
    if type(version) is int and version in (1, 2):
        assert all(_validate_sensor(payload, frame_directory).values())
    else:
        with pytest.raises(capture.VtolMissionSensorCaptureError, match="manifest_content_bound"):
            _validate_sensor(payload, frame_directory)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    (
        ("qualified", False, "qualified"),
        ("wire_version", 1, "wire_v3"),
        ("source_row_order", "bottom_to_top", "row_order_normalized"),
        ("wire_row_order", "bottom_to_top", "row_order_normalized"),
        ("vertical_flip_applied", True, "row_order_normalized"),
        ("transport.vertical_flip_applied", True, "row_order_normalized"),
        ("truth_free", False, "truth_free"),
        ("full_odometry_pose_velocity_fusion_claimed", False, "full_odometry"),
        ("requested_frame_count", 2, "frame_count"),
        ("pixel_persistence.status", "planned_not_executed", "pixels_persisted"),
        ("pixel_persistence.persisted_frame_count", 2, "pixels_persisted"),
        ("pixel_persistence.actual_gazebo_pixels_persisted", False, "pixels_persisted"),
        ("pixel_persistence.raw_bytes_sha256_matches_wire_payload", False, "pixels_persisted"),
        ("pixel_persistence.qgc_rendering", "passed", "no_qgc_render_claim"),
        ("pixel_persistence.manual_visual_acceptance", "passed", "no_manual_visual_claim"),
        ("pixel_persistence.hardware", "passed", "no_hardware_claim"),
    ),
)
def test_wire_v3_capture_fails_closed_on_non_persisted_or_overclaimed_pixels(
    tmp_path: Path,
    path: str,
    value: Any,
    message: str,
) -> None:
    frame_directory = tmp_path / "frames"
    payload = _set_nested(_sensor_payload(frame_directory), path, value)
    with pytest.raises(capture.VtolMissionSensorCaptureError, match=message):
        _validate_sensor(payload, frame_directory)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_row_order", "bottom_to_top"),
        ("wire_row_order", "bottom_to_top"),
        ("vertical_flip_applied", True),
    ),
)
def test_wire_v3_capture_rejects_rehashed_manifest_row_order_tampering(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    frame_directory = tmp_path / "frames"
    payload = _sensor_payload(frame_directory)
    manifest_path = frame_directory / capture.GAZEBO_BGR24_FRAME_SET_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_attribution"][field] = value
    manifest_without_hash = dict(manifest)
    manifest_without_hash.pop("manifest_sha256")
    manifest["manifest_sha256"] = canonical_sha256(manifest_without_hash)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_record = payload["pixel_persistence"]["manifest"]
    manifest_record["bytes"] = manifest_path.stat().st_size
    manifest_record["file_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest_record["manifest_sha256"] = manifest["manifest_sha256"]

    with pytest.raises(capture.VtolMissionSensorCaptureError, match="manifest_content_bound"):
        _validate_sensor(payload, frame_directory)


@pytest.mark.parametrize("tamper", ("raw-frame", "manifest", "extra-file"))
def test_wire_v3_capture_rehashes_real_files_and_sealed_directory(
    tmp_path: Path,
    tamper: str,
) -> None:
    frame_directory = tmp_path / "frames"
    payload = _sensor_payload(frame_directory)
    if tamper == "raw-frame":
        (frame_directory / "frame-000001.bgr24").write_bytes(b"tampered")
        message = "frame_files_integrity"
    elif tamper == "manifest":
        manifest_path = frame_directory / capture.GAZEBO_BGR24_FRAME_SET_MANIFEST
        manifest_path.write_text("{}\n", encoding="utf-8")
        message = "manifest_file_integrity"
    else:
        (frame_directory / "unexpected.bin").write_bytes(b"unexpected")
        message = "exact_directory_contents"
    with pytest.raises(capture.VtolMissionSensorCaptureError, match=message):
        _validate_sensor(payload, frame_directory)


@pytest.mark.parametrize(
    ("ready", "started", "completed"),
    (
        (float("nan"), 1_000.1, 1_000.2),
        (1_000.0, 999.9, 1_000.2),
        (1_000.0, 1_006.0, 1_006.1),
        (1_000.0, 1_000.2, 1_000.1),
    ),
)
def test_wire_v3_capture_rejects_invalid_or_unbounded_barrier_timing(
    tmp_path: Path,
    ready: float,
    started: float,
    completed: float,
) -> None:
    frame_directory = tmp_path / "frames"
    payload = _sensor_payload(frame_directory)
    with pytest.raises(
        capture.VtolMissionSensorCaptureError,
        match="capture_after_current_fw_barrier",
    ):
        capture.validate_sensor_capture(
            payload,
            frame_count=3,
            frame_directory=frame_directory,
            scenario_id=capture.EXPECTED_SCENARIO_ID,
            scenario_manifest_hash=SCENARIO_HASH,
            expected_width=2,
            expected_height=2,
            flight_ready_monotonic_s=ready,
            capture_started_monotonic_s=started,
            capture_completed_monotonic_s=completed,
        )


@pytest.mark.parametrize(
    ("started", "completed"),
    (
        (READY_MONOTONIC_S + 0.101, READY_MONOTONIC_S + 0.2),
        (READY_MONOTONIC_S + 0.1, READY_MONOTONIC_S + 0.199),
    ),
)
def test_wire_v3_capture_rejects_frames_outside_outer_capture_interval(
    tmp_path: Path,
    started: float,
    completed: float,
) -> None:
    frame_directory = tmp_path / "frames"
    payload = _sensor_payload(frame_directory)

    with pytest.raises(
        capture.VtolMissionSensorCaptureError,
        match="frame_timestamps_within_capture_interval",
    ):
        capture.validate_sensor_capture(
            payload,
            frame_count=3,
            frame_directory=frame_directory,
            scenario_id=capture.EXPECTED_SCENARIO_ID,
            scenario_manifest_hash=SCENARIO_HASH,
            expected_width=2,
            expected_height=2,
            flight_ready_monotonic_s=READY_MONOTONIC_S,
            capture_started_monotonic_s=started,
            capture_completed_monotonic_s=completed,
        )


def test_router_result_requires_ready_then_graceful_stop_and_forwarded_mission() -> None:
    checks = capture.validate_router_result(_router_payload(), ports=_ports())
    assert all(checks.values())


@pytest.mark.parametrize(
    ("path", "value", "message"),
    (
        ("termination_reason", "duration_elapsed", "graceful_stop"),
        ("ready_barrier.reached", False, "ready"),
        ("ready_barrier.bound_udp_socket_count", 2, "ready"),
        ("endpoints.evaluator", "127.0.0.1:59999", "endpoints_bound"),
        ("evaluator_identity.system_id", 249, "evaluator_identity_bound"),
        ("px4_frames_forwarded_to_evaluator", 0, "telemetry_forwarded"),
        ("evaluator_mission_frames_forwarded", 0, "mission_frames_forwarded"),
        ("evaluator_control_commands_forwarded", 0, "control_forwarded"),
        ("px4_discarded_bytes", 1, "no_discarded_bytes"),
        ("all_udp_endpoints_loopback", False, "loopback"),
        ("protected_udp_14550_used", True, "protected_port_unused"),
        ("evaluator_arbitrary_commands_allowed", True, "bounded_evaluator_policy"),
        ("physical_actuator_outputs_present", True, "software_only"),
    ),
)
def test_router_result_fails_closed_on_unready_ungraceful_or_lossy_state(
    path: str,
    value: Any,
    message: str,
) -> None:
    payload = _set_nested(_router_payload(), path, value)
    with pytest.raises(capture.VtolMissionSensorCaptureError, match=message):
        capture.validate_router_result(payload, ports=_ports())


def test_router_thread_uses_all_six_ports_and_stops_via_event() -> None:
    observed: dict[str, Any] = {}

    def router_runner(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        kwargs["ready_event"].set()
        assert kwargs["stop_event"].wait(2.0)
        return _router_payload()

    handle = capture._start_router_thread(
        ports=_ports(),
        mission_timeout_s=45.0,
        router_runner=router_runner,
    )
    capture._wait_router_ready(handle, 2.0)
    record = capture._stop_router(handle, timeout_s=2.0)

    assert record["ready"] is True
    assert record["stopped"] is True
    assert record["error"] is None
    assert capture.validate_router_result(record["result"], ports=_ports())
    assert observed["px4_input_port"] == 51_001
    assert observed["px4_telemetry_port"] == 51_002
    assert observed["qgc_port"] == 51_003
    assert observed["qgc_router_port"] == 51_004
    assert observed["evaluator_port"] == 51_005
    assert observed["evaluator_router_port"] == 51_006
    assert observed["acknowledge_owned_disposable_sitl"] is True
    assert observed["duration_seconds"] == 165.0
    assert observed["duration_limit_seconds"] == 165.0


@pytest.mark.parametrize("reference_profile", ["lifecycle-smoke", "fixed-wing-response"])
def test_mission_helper_arguments_bind_exact_container_and_flight_ready_file(
    tmp_path: Path,
    reference_profile: str,
) -> None:
    paths = capture.CapturePaths(
        run_directory=tmp_path / "run",
        evidence=tmp_path / "evidence.json",
        flight_ready=tmp_path / "ready.json",
        flight_capture_complete=tmp_path / "complete.json",
        mission_journal=tmp_path / "mission.jsonl",
        mission_evidence=tmp_path / "mission-evidence.json",
        mission_stdout=tmp_path / "mission.stdout.jsonl",
        mission_stderr=tmp_path / "mission.stderr.log",
        frame_directory=tmp_path / "frames",
        docker_job_directory=tmp_path / "docker-plan",
    )
    arguments = capture._process_arguments(
        paths=paths,
        scenario=SCENARIO,
        container_name="human-readable-name",
        container_id=CONTAINER_ID,
        evaluator_port=51_005,
        airframe_profile_sha256="1" * 64,
        runtime_descriptor_sha256=DESCRIPTOR_HASH,
        runtime_provenance_sha256=PROVENANCE_HASH,
        mission_timeout_s=90.0,
        reference_profile=reference_profile,
        docker_executable=r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    )

    assert arguments[arguments.index("--expected-container-id") + 1] == CONTAINER_ID
    assert arguments[arguments.index("--container-name") + 1] == "human-readable-name"
    assert arguments[arguments.index("--flight-capture-ready-file") + 1] == str(paths.flight_ready)
    assert arguments[arguments.index("--flight-capture-complete-file") + 1] == str(
        paths.flight_capture_complete
    )
    assert arguments[arguments.index("--evaluator-port") + 1] == "51005"
    assert arguments[arguments.index("--docker") + 1] == (
        r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    )
    assert "--acknowledge-owned-disposable-sitl" in arguments
    if reference_profile == "fixed-wing-response":
        assert arguments[arguments.index("--reference-profile") + 1] == reference_profile
    else:
        assert "--reference-profile" not in arguments


def test_campaign_sensor_capture_keeps_first_sortie_barriers_and_owns_journal(
    tmp_path: Path,
) -> None:
    """Combined campaign mode must retain only the paired first-sortie barriers."""

    paths = capture.CapturePaths(
        run_directory=tmp_path / "run",
        evidence=tmp_path / "evidence.json",
        flight_ready=tmp_path / "ready.json",
        flight_capture_complete=tmp_path / "complete.json",
        mission_journal=tmp_path / "mission.jsonl",
        mission_evidence=tmp_path / "mission-evidence.json",
        mission_stdout=tmp_path / "mission.stdout.jsonl",
        mission_stderr=tmp_path / "mission.stderr.log",
        frame_directory=tmp_path / "frames",
        docker_job_directory=tmp_path / "docker-plan",
    )
    arguments = capture._process_arguments(
        paths=paths,
        scenario=SCENARIO,
        container_name="combined-campaign",
        container_id=CONTAINER_ID,
        evaluator_port=51_005,
        airframe_profile_sha256=AIRFRAME_HASH,
        runtime_descriptor_sha256=DESCRIPTOR_HASH,
        runtime_provenance_sha256=PROVENANCE_HASH,
        mission_timeout_s=90.0,
        docker_executable="docker-test",
        campaign_file=tmp_path / "campaign.json",
        capture_campaign_sensors=True,
    )

    assert arguments[1:3] == ["-m", "scripts.vtol_selected_task_campaign"]
    assert arguments[arguments.index("--flight-capture-ready-file") + 1] == str(
        paths.flight_ready
    )
    assert arguments[arguments.index("--flight-capture-complete-file") + 1] == str(
        paths.flight_capture_complete
    )
    assert "--mission-journal" not in arguments
    assert "--campaign-file" in arguments
    assert "--campaign-output-directory" in arguments


def test_combined_campaign_rejects_missing_campaign_or_prebound_manifest_before_external_work(
    tmp_path: Path,
) -> None:
    calls: list[object] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(object())
        raise AssertionError("external work started before validation")

    with pytest.raises(capture.VtolMissionSensorCaptureError, match="requires campaign"):
        capture.run_vtol_mission_sensor_capture(
            SCENARIO,
            tmp_path / "missing-campaign-evidence.json",
            run_directory=tmp_path / "missing-campaign-run",
            execute=True,
            capture_campaign_sensors=True,
            command_runner=forbidden,
        )

    campaign_file = tmp_path / "campaign.json"
    campaign_file.write_text("{}", encoding="utf-8")
    with pytest.raises(
        capture.VtolMissionSensorCaptureError,
        match="cannot prebind a sensor manifest",
    ):
        capture.run_vtol_mission_sensor_capture(
            SCENARIO,
            tmp_path / "prebound-evidence.json",
            run_directory=tmp_path / "prebound-run",
            execute=True,
            campaign_file=campaign_file,
            capture_campaign_sensors=True,
            sensor_transport_manifest=tmp_path / "manifest.json",
            sensor_frame_map=tmp_path / "frames.json",
            command_runner=forbidden,
        )

    assert calls == []
    assert not (tmp_path / "missing-campaign-run").exists()
    assert not (tmp_path / "prebound-run").exists()


def test_reference_profile_rejected_before_external_work(tmp_path):
    for options in (
        {"reference_profile": "typo"},
        {"reference_profile": "fixed-wing-response", "campaign_file": tmp_path / "missing.json"},
    ):
        with pytest.raises(capture.VtolMissionSensorCaptureError, match="reference profile"):
            capture.run_vtol_mission_sensor_capture(execute=True, **options)


def test_exact_id_cleanup_never_targets_mutable_container_name() -> None:
    commands: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        returncode = 1 if argv[1:3] == ["container", "inspect"] else 0
        stderr = (
            f"Error response from daemon: No such container: {CONTAINER_ID}" if returncode else ""
        )
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr=stderr)

    result = capture._cleanup_exact_container(
        docker="docker-test",
        container_id=CONTAINER_ID,
        runner=runner,
    )

    assert result["removed"] is True
    assert result["cleanup_succeeded"] is True
    assert result["post_remove_absence_proven"] is True
    assert result["exact_id"] == CONTAINER_ID
    assert commands == [
        ["docker-test", "stop", "--timeout", "2", CONTAINER_ID],
        ["docker-test", "rm", CONTAINER_ID],
        ["docker-test", "container", "inspect", CONTAINER_ID],
    ]


def test_exact_id_cleanup_rejects_daemon_failure_as_absence_proof() -> None:
    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = argv[1:]
        if command[:2] == ["container", "inspect"]:
            return subprocess.CompletedProcess(
                argv,
                125,
                stdout="",
                stderr="Cannot connect to the Docker daemon",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result = capture._cleanup_exact_container(
        docker="docker-test",
        container_id=CONTAINER_ID,
        runner=runner,
    )

    assert result["removed"] is False
    assert result["cleanup_succeeded"] is False
    assert result["post_remove_absence_proven"] is False
    assert "did not prove exact container absence" in " ".join(result["errors"])


@pytest.mark.parametrize("qpc", [False, True])
def test_fake_full_lifecycle_binds_pixels_mission_and_cleanup_to_exact_container_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qpc: bool,
) -> None:
    docker_calls: list[list[str]] = []
    capture_calls: list[dict[str, Any]] = []
    mission_arguments: list[str] = []

    def command_runner(
        argv: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        docker_calls.append(argv)
        command = argv[1:]
        if command[:2] == ["container", "inspect"]:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr=f"Error response from daemon: No such container: {CONTAINER_ID}",
            )
        if command and command[0] == "run":
            return subprocess.CompletedProcess(argv, 0, stdout=f"{CONTAINER_ID}\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def inspect_container(
        _docker: str,
        target: str,
        *,
        runner: Any,
    ) -> dict[str, Any]:
        assert runner is command_runner
        assert target == CONTAINER_ID
        return {"Id": CONTAINER_ID}

    def validate_runtime_container(
        *,
        record: dict[str, Any],
        plan: Any,
        container_id: str,
    ) -> dict[str, Any]:
        assert record["Id"] == CONTAINER_ID
        assert container_id == CONTAINER_ID
        assert plan.container_name == "multidetect-vtol-fake-lifecycle"
        return {"docker_acceptance": {"all": True}, "vtol_mission": {"all": True}}

    class FakeMissionProcess:
        def __init__(self, arguments: list[str], **_kwargs: Any) -> None:
            mission_arguments.extend(arguments)
            self._arguments = arguments
            self._complete = False
            ready_path = Path(arguments[arguments.index("--flight-capture-ready-file") + 1])
            ready_payload = _flight_ready_payload()
            from multidetect.clock import HighResolutionSystemClock

            self.clock = (
                HighResolutionSystemClock(
                    session_id=arguments[arguments.index("--host-clock-session-id") + 1]
                )
                if qpc
                else None
            )
            ready_payload["captured_at_monotonic_s"] = (
                self.clock.monotonic() if qpc else capture.time.monotonic()
            )
            if qpc:
                ready_payload["captured_at_clock"] = "host_perf_counter"
                ready_payload["host_clock_identity"] = self.clock.identity()
            ready_path.write_text(
                json.dumps(ready_payload),
                encoding="utf-8",
            )

        def poll(self) -> int | None:
            return 0 if self._complete else None

        def wait(self, timeout: float) -> int:
            assert timeout > 0
            mission = _mission_payload(
                descriptor_hash=self._arguments[
                    self._arguments.index("--runtime-descriptor-sha256") + 1
                ],
                provenance_hash=self._arguments[
                    self._arguments.index("--runtime-provenance-sha256") + 1
                ],
                airframe_hash=self._arguments[
                    self._arguments.index("--airframe-profile-sha256") + 1
                ],
            )
            scenario_path = Path(self._arguments[self._arguments.index("--scenario-manifest") + 1])
            mission["scenario"]["manifest_sha256"] = hashlib.sha256(
                scenario_path.read_bytes()
            ).hexdigest()
            evaluator_port = self._arguments[self._arguments.index("--evaluator-port") + 1]
            mission["mavlink"]["endpoint"] = f"udpin:127.0.0.1:{evaluator_port}"
            ready_path = Path(
                self._arguments[self._arguments.index("--flight-capture-ready-file") + 1]
            )
            complete_path = Path(
                self._arguments[self._arguments.index("--flight-capture-complete-file") + 1]
            )
            complete_document = json.loads(complete_path.read_text(encoding="utf-8"))
            flight = mission["flight"]
            flight["flight_capture_ready_file"] = str(ready_path)
            flight["flight_capture_complete_file"] = str(complete_path)
            flight["flight_capture_complete_file_sha256"] = hashlib.sha256(
                complete_path.read_bytes()
            ).hexdigest()
            flight["flight_capture_complete_document"] = complete_document
            interval_start = complete_document["capture_started_at_monotonic_s"]
            interval_complete = complete_document["capture_completed_at_monotonic_s"]
            if qpc:
                assert complete_document["host_clock_identity"] == self.clock.identity()
                assert complete_document["clock"] == "host_perf_counter"
                for sample in flight["flight_capture_interval_samples"]:
                    sample["captured_at_clock"] = "host_perf_counter"
                    sample["host_clock_identity"] = self.clock.identity()
            flight["flight_capture_interval_samples"][0]["captured_at_monotonic_s"] = interval_start
            flight["flight_capture_interval_samples"][-1]["captured_at_monotonic_s"] = (
                interval_complete
            )
            journal_path = Path(self._arguments[self._arguments.index("--mission-journal") + 1])
            mission["mission_journal"] = _write_completed_mission_journal(
                journal_path,
                mission_payload=mission,
            )
            mission_path = Path(self._arguments[self._arguments.index("--evidence") + 1])
            mission_path.write_text(json.dumps(mission), encoding="utf-8")
            self._complete = True
            return 0

        def terminate(self) -> None:
            raise AssertionError("completed fake mission must not be terminated")

        def kill(self) -> None:
            raise AssertionError("completed fake mission must not be killed")

    def router_runner(**kwargs: Any) -> dict[str, Any]:
        kwargs["ready_event"].set()
        assert kwargs["stop_event"].wait(5.0)
        return _router_payload()

    def sensor_capture(**kwargs: Any) -> dict[str, Any]:
        capture_calls.append(kwargs)
        assert kwargs["plan"].container_name == CONTAINER_ID
        assert len(kwargs["plan"].container_name) == 64
        return _sensor_payload(
            Path(kwargs["frame_output_directory"]),
            frame_count=kwargs["frame_count"],
            width=kwargs["expected_width"],
            height=kwargs["expected_height"],
            scenario_id=kwargs["plan"].scenario_id,
            scenario_manifest_hash=kwargs["plan"].scenario_manifest_hash,
            live_timestamps=True,
            clock=kwargs.get("clock"),
        )

    monkeypatch.setattr(capture, "_container_inspect", inspect_container)
    monkeypatch.setattr(capture, "_validate_runtime_container", validate_runtime_container)

    evidence_path = tmp_path / "evidence.json"
    result = capture.run_vtol_mission_sensor_capture(
        SCENARIO,
        evidence_path,
        run_directory=tmp_path / "run",
        container_name="multidetect-vtol-fake-lifecycle",
        host_clock="perf_counter" if qpc else "monotonic",
        execute=True,
        frame_count=3,
        frame_width=2,
        frame_height=2,
        depth_output_directory=tmp_path / "depth-frames",
        mission_timeout_s=30.0,
        ready_timeout_s=10.0,
        ports=_ports(),
        port_probe=lambda _port: True,
        docker="docker-test",
        command_runner=command_runner,
        process_factory=FakeMissionProcess,
        router_runner=router_runner,
        sensor_capture=sensor_capture,
    )

    assert result["gate_passed"] is True, result
    assert all(result["gates"].values())
    assert result["container"]["id"] == CONTAINER_ID
    assert result["sensor_capture"]["docker_exec_target"] == CONTAINER_ID
    assert result["sensor_capture"]["docker_exec_target_is_exact_id"] is True
    assert result["sensor_capture"]["same_container_as_flight_barrier"] is True
    assert capture_calls[0]["plan"].container_name == CONTAINER_ID
    assert capture_calls[0]["depth_output_directory"] == (tmp_path / "depth-frames").resolve()
    assert mission_arguments[mission_arguments.index("--expected-container-id") + 1] == CONTAINER_ID
    assert docker_calls[-3:] == [
        ["docker-test", "stop", "--timeout", "2", CONTAINER_ID],
        ["docker-test", "rm", CONTAINER_ID],
        ["docker-test", "container", "inspect", CONTAINER_ID],
    ]
    assert result["execution"]["qgc_process_started"] is False
    assert result["execution"]["computer_use_invoked"] is False


def test_combined_campaign_fake_lifecycle_captures_first_sortie_and_fails_closed_on_pixel_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The campaign barrier/capture path must not publish tampered pixels."""

    from scripts import vtol_selected_task_campaign as campaign

    campaign_file = tmp_path / "campaign.json"
    campaign_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        campaign,
        "load_campaign_definition",
        lambda _path: ([object()], {}, None),
    )

    docker_calls: list[list[str]] = []
    capture_calls: list[dict[str, Any]] = []
    campaign_validation_calls = 0
    frame_directory: Path | None = None

    def command_runner(
        argv: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        docker_calls.append(argv)
        command = argv[1:]
        if command[:2] == ["container", "inspect"]:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr=f"Error response from daemon: No such container: {CONTAINER_ID}",
            )
        if command and command[0] == "run":
            return subprocess.CompletedProcess(argv, 0, stdout=f"{CONTAINER_ID}\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def inspect_container(
        _docker: str,
        target: str,
        *,
        runner: Any,
    ) -> dict[str, Any]:
        assert runner is command_runner
        assert target == CONTAINER_ID
        return {"Id": CONTAINER_ID}

    def validate_runtime_container(
        *,
        record: dict[str, Any],
        plan: Any,
        container_id: str,
    ) -> dict[str, Any]:
        assert record["Id"] == CONTAINER_ID
        assert container_id == CONTAINER_ID
        assert plan.container_name == "combined-campaign-fake"
        return {"docker_acceptance": {"all": True}, "vtol_mission": {"all": True}}

    def wait_for_ready(*, path: Path, **_kwargs: Any):
        payload = _flight_ready_payload()
        payload["captured_at_monotonic_s"] = capture.time.monotonic()
        binding = {
            "schema": "multidetect.vtol-campaign-flight-capture-binding",
            "schema_version": 1,
            "task_id": "synthetic-east",
            "context_sha256": "d" * 64,
            "plan_sha256": "a" * 64,
            "executable_mission_sha256": "b" * 64,
            "wire_hash": "c" * 64,
            "airframe_profile_sha256": "e" * 64,
            "transaction_id": None,
            "transaction_binding": "ready_marker_and_mission_evidence",
            "policy_applied_to_actuator": False,
            "truth_free": True,
        }
        payload["task_binding"] = {
            key: binding[key]
            for key in ("task_id", "plan_sha256", "executable_mission_sha256", "wire_hash")
        }
        descriptor_path = path.parent / "campaign-missions" / "mission-001.binding.json"
        descriptor_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor_path.write_text(json.dumps(binding), encoding="utf-8")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload, {"all": True}

    class FakeCampaignProcess:
        def __init__(self, arguments: list[str], **_kwargs: Any) -> None:
            self.arguments = arguments
            self.completed = False

        def poll(self) -> int | None:
            return 0 if self.completed else None

        def wait(self, timeout: float) -> int:
            assert timeout > 0
            campaign_directory = Path(
                self.arguments[self.arguments.index("--campaign-output-directory") + 1]
            )
            campaign_directory.mkdir(parents=True, exist_ok=True)
            (campaign_directory / "mission-001.json").write_text("{}", encoding="utf-8")
            (campaign_directory / "mission-001.transaction.jsonl").write_text(
                "fixture\n", encoding="utf-8"
            )
            Path(self.arguments[self.arguments.index("--evidence") + 1]).write_text(
                "{}", encoding="utf-8"
            )
            self.completed = True
            return 0

        def terminate(self) -> None:
            raise AssertionError("completed fake campaign must not be terminated")

        def kill(self) -> None:
            raise AssertionError("completed fake campaign must not be killed")

    def router_runner(**kwargs: Any) -> dict[str, Any]:
        kwargs["ready_event"].set()
        assert kwargs["stop_event"].wait(5.0)
        return _router_payload()

    def sensor_capture(**kwargs: Any) -> dict[str, object]:
        nonlocal frame_directory
        capture_calls.append(kwargs)
        frame_directory = Path(kwargs["frame_output_directory"])
        return _sensor_payload(
            frame_directory,
            frame_count=kwargs["frame_count"],
            width=kwargs["expected_width"],
            height=kwargs["expected_height"],
            scenario_id=kwargs["plan"].scenario_id,
            scenario_manifest_hash=kwargs["plan"].scenario_manifest_hash,
            live_timestamps=True,
        )

    def campaign_result(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal campaign_validation_calls
        campaign_validation_calls += 1
        if campaign_validation_calls == 1:
            assert frame_directory is not None
            tampered = next(frame_directory.glob("frame-*.bgr24"))
            tampered.write_bytes(b"tampered-pixel")
        return {"status": "all_tasks_completed", "tamper_check": campaign_validation_calls}

    monkeypatch.setattr(capture, "_container_inspect", inspect_container)
    monkeypatch.setattr(capture, "_validate_runtime_container", validate_runtime_container)
    monkeypatch.setattr(capture, "_wait_for_flight_ready", wait_for_ready)
    monkeypatch.setattr(capture, "validate_flight_capture_ready", lambda *a, **k: {"all": True})
    monkeypatch.setattr(
        capture,
        "validate_mission_evidence",
        lambda *a, **k: {
            "flight_capture_interval": True,
            "vtol_landed": True,
            "terminal_disarmed": True,
        },
    )
    monkeypatch.setattr(capture, "validate_mission_journal", lambda *a, **k: {"all": True})
    monkeypatch.setattr(capture, "validate_campaign_result", campaign_result)

    result = capture.run_vtol_mission_sensor_capture(
        SCENARIO,
        tmp_path / "combined-evidence.json",
        run_directory=tmp_path / "combined-run",
        container_name="combined-campaign-fake",
        campaign_file=campaign_file,
        capture_campaign_sensors=True,
        execute=True,
        frame_count=3,
        frame_width=2,
        frame_height=2,
        mission_timeout_s=30.0,
        ready_timeout_s=10.0,
        ports=_ports(),
        port_probe=lambda _port: True,
        docker="docker-test",
        energy_configuration=_energy_configuration(),
        command_runner=command_runner,
        process_factory=FakeCampaignProcess,
        router_runner=router_runner,
        sensor_capture=sensor_capture,
    )

    assert capture_calls[0]["plan"].container_name == CONTAINER_ID
    assert campaign_validation_calls >= 1
    assert result["gate_passed"] is False
    assert result["gates"]["same_container_sensor_capture"] is True
    assert result["gates"]["wire_v3_pixels"] is True
    assert result["gates"]["final_artifact_integrity"] is False
    assert "sensor capture checks failed" in result["failure"]
    assert docker_calls[-3:] == [
        ["docker-test", "stop", "--timeout", "2", CONTAINER_ID],
        ["docker-test", "rm", CONTAINER_ID],
        ["docker-test", "container", "inspect", CONTAINER_ID],
    ]


def test_dry_run_writes_truthful_nonpassing_evidence_without_external_execution(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("external")
        raise AssertionError("dry-run attempted an external process or runtime channel")

    evidence_path = tmp_path / "evidence.json"
    result = capture.run_vtol_mission_sensor_capture(
        SCENARIO,
        evidence_path,
        run_directory=tmp_path / "run",
        execute=False,
        ports=_ports(),
        port_probe=lambda _port: True,
        command_runner=forbidden,
        process_factory=forbidden,
        router_runner=forbidden,
        sensor_capture=forbidden,
    )

    assert calls == []
    assert result["gate_passed"] is False
    assert result["execution"] == {
        "requested": False,
        "status": "planned_not_executed",
        "lifecycle_owner": "scripts/run_vtol_mission_sensor_capture.py",
        "headless": True,
        "qgc_process_started": False,
        "computer_use_invoked": False,
    }
    assert result["container"]["started"] is False
    assert result["container"]["id"] is None
    assert result["router"]["started"] is False
    assert result["gates"]["docker_plan"] is True
    assert sum(result["gates"].values()) == 1
    assert result["safety"]["qgc_started"] is False
    assert result["safety"]["manual_visual_acceptance"] == "not_claimed"
    assert result["safety"]["hardware"] == "not_claimed"
    written = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert written == result
    stored_hash = written.pop("evidence_sha256")
    assert stored_hash == canonical_sha256(written)


def test_exact_container_id_is_the_sensor_docker_exec_target() -> None:
    source = capture.SCRIPT if hasattr(capture, "SCRIPT") else Path(capture.__file__)
    text = Path(source).read_text(encoding="utf-8")

    assert "capture_plan = replace(plan, container_name=container_id)" in text
    assert "plan=capture_plan" in text
    assert '"docker_exec_target": capture_plan.container_name' in text
    assert '"docker_exec_target_is_exact_id": True' in text
    assert 'flight_ready.get("container", {}).get("id") == container_id' in text


def test_module_has_no_qgc_or_computer_use_launch_surface() -> None:
    source = Path(capture.__file__).read_text(encoding="utf-8")

    assert "MultiDetectGCS.exe" not in source
    assert "QApplication(" not in source
    assert "mcp__computer" not in source
    assert '"qgc_process_started": False' in source
    assert '"computer_use_invoked": False' in source


def test_capture_cli_forwards_explicit_ready_and_complete_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return {"gate_passed": False, "execution": {"status": "planned_not_executed"}}

    monkeypatch.setattr(capture, "run_vtol_mission_sensor_capture", fake_run)
    ready = tmp_path / "ready.json"
    complete = tmp_path / "complete.json"
    code = capture.main(
        [
            "--scenario",
            str(SCENARIO),
            "--evidence",
            str(tmp_path / "evidence.json"),
            "--run-directory",
            str(tmp_path / "run"),
            "--flight-capture-ready-file",
            str(ready),
            "--flight-capture-complete-file",
            str(complete),
        ]
    )

    assert code == 0
    assert observed["kwargs"]["flight_ready_file"] == ready
    assert observed["kwargs"]["flight_capture_complete_file"] == complete
    assert json.loads(capsys.readouterr().out)["gate_passed"] is False

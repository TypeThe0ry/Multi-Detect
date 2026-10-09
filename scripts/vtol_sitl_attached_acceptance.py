#!/usr/bin/env python3
"""Drive the VTOL state gate through an already-owned combined SIM container.

This helper deliberately does not create, stop, or remove a container.  The
top-level combined acceptance runner owns that lifecycle and presents a
loopback-only evaluator MAVLink endpoint.  The helper attaches to that endpoint,
observes the real PX4/Gazebo state, applies four tightly bounded test commands
(ARM, FW, MC, DISARM), and writes standalone evidence.  Product runtime code is
not given this command channel.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from vtol_sitl_scenario_acceptance import (
    GCS_SOURCE_COMPONENT,
    GCS_SOURCE_SYSTEM,
    TelemetryCollector,
    VtolAcceptanceError,
    _container_inspect,
    _docker_path,
    _fault_timeline,
    _position_quality,
    _run,
    _send_command,
    _sha256_path,
    _utc_now,
    mavutil,
)

from multidetect.product_version import PRODUCT_VERSION
from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario, load_scenario_manifest

DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "scenarios"
    / "river_rescue_vtol_001.scenario.json"
)
EXPECTED_CONTAINER_LABEL = "combined-px4-gazebo-qgc-sim-acceptance"
PROTECTED_GROUND_STATION_PORT = 14550
RELEASE_STATUS = (
    f"v{PRODUCT_VERSION.release_label}; final v{PRODUCT_VERSION.numeric} remains blocked"
)
LANDED_STATE_SETTLE_TIMEOUT_SECONDS = 20.0
# Docker Desktop can briefly starve PX4's MAVLink heartbeat stream while the
# evaluator command is being applied.  COMMAND_ACK=ACCEPTED is not a state
# proof, so keep waiting for a fresh armed heartbeat, but use a bounded window
# that covers the observed cold-start latency instead of racing at eight
# seconds.
ARMED_STATE_TIMEOUT_SECONDS = 30.0


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _wait_for_landed_disarmed_state(collector: TelemetryCollector) -> None:
    """Wait for PX4's land detector to settle across the evaluator boundary.

    An unarmed heartbeat can arrive while PX4 still reports TAKEOFF/LANDING.
    The combined runner starts the Mission transaction immediately afterwards;
    carrying that transient state across the boundary can leave AUTO.MISSION
    armed but motionless.  Keep this barrier bounded and fail closed.
    """
    deadline = collector.elapsed_s + LANDED_STATE_SETTLE_TIMEOUT_SECONDS
    while collector.elapsed_s < deadline:
        collector.collect(min(0.25, max(0.0, deadline - collector.elapsed_s)))
        heartbeat = collector.last_heartbeat
        if (
            heartbeat
            and not (
                int(heartbeat["base_mode"])
                & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
            )
            and collector.last_landed_state == mavutil.mavlink.MAV_LANDED_STATE_ON_GROUND
        ):
            return
    raise VtolAcceptanceError(
        "attached VTOL evaluator disarmed but PX4 land detector did not settle "
        "to MAV_LANDED_STATE_ON_GROUND"
    )


def _validate_owned_running_container(
    docker: str,
    container_name: str,
    *,
    expected_label: str,
) -> tuple[str, dict[str, Any]]:
    record = _container_inspect(docker, container_name)
    config = record.get("Config") or {}
    state = record.get("State") or {}
    labels = config.get("Labels") or {}
    container_id = str(record.get("Id") or "")
    if not container_id:
        raise VtolAcceptanceError("combined SIM container has no Docker ID")
    if record.get("Name") != f"/{container_name}":
        raise VtolAcceptanceError("combined SIM container name does not match")
    if state.get("Running") is not True:
        raise VtolAcceptanceError("combined SIM container is not running")
    if labels.get("multidetect.purpose") != expected_label:
        raise VtolAcceptanceError("combined SIM container ownership label does not match")
    return container_id, record


def run_attached_acceptance(
    manifest_path: Path,
    evidence_path: Path,
    *,
    docker: str,
    container_name: str,
    evaluator_port: int,
    expected_container_label: str = EXPECTED_CONTAINER_LABEL,
    fault_time_scale: float = 0.01,
    transition_timeout_s: float = 15.0,
    fixed_wing_hold_s: float = 8.0,
    multicopter_hold_s: float = 1.0,
    armed_state_timeout_s: float = ARMED_STATE_TIMEOUT_SECONDS,
    fixed_wing_ready_file: Path | None = None,
    source_system: int = GCS_SOURCE_SYSTEM,
    source_component: int = GCS_SOURCE_COMPONENT,
) -> dict[str, Any]:
    """Attach to the evaluator endpoint and execute the bounded VTOL gate."""

    if not 1024 <= evaluator_port <= 65535 or evaluator_port == PROTECTED_GROUND_STATION_PORT:
        raise VtolAcceptanceError("evaluator port must be in 1024..65535 and cannot be 14550")
    for value, name, minimum, maximum in (
        (fault_time_scale, "fault_time_scale", 0.000001, 1.0),
        (transition_timeout_s, "transition_timeout_s", 1.0, 120.0),
        (fixed_wing_hold_s, "fixed_wing_hold_s", 0.0, 60.0),
        (multicopter_hold_s, "multicopter_hold_s", 0.0, 60.0),
        (armed_state_timeout_s, "armed_state_timeout_s", 1.0, 120.0),
    ):
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise VtolAcceptanceError(f"{name} must be finite in [{minimum}, {maximum}]")
    if not 1 <= source_system <= 255 or not 1 <= source_component <= 255:
        raise VtolAcceptanceError("MAVLink source system/component must be in 1..255")

    source = manifest_path.resolve(strict=True)
    runtime_input = load_runtime_scenario(source, mode=RuntimeMode.SIM)
    evaluation_manifest = load_scenario_manifest(source)
    if runtime_input.vehicle.model != "px4_vtol_standard":
        raise VtolAcceptanceError("attached gate requires the px4_vtol_standard runtime model")

    container_id, _record = _validate_owned_running_container(
        docker,
        container_name,
        expected_label=expected_container_label,
    )
    endpoint = f"udpin:127.0.0.1:{evaluator_port}"
    connection: Any | None = None
    collector: TelemetryCollector | None = None
    command_events: list[dict[str, Any]] = []
    phase_events: list[dict[str, Any]] = []
    parameter_overrides: dict[str, Any] = {}
    fault_timeline = _fault_timeline(runtime_input, fault_time_scale)
    failure: str | None = None
    try:
        connection = mavutil.mavlink_connection(
            endpoint,
            source_system=source_system,
            source_component=source_component,
            autoreconnect=False,
        )
        collector = TelemetryCollector(connection)
        heartbeat = collector.wait_for_heartbeat(45.0)
        phase_events.append({"phase": "boot", "heartbeat": heartbeat, "at_s": 0.0})
        collector.wait_for_gps_fix(25.0)
        collector.wait_for_navigation_settle()

        for parameter, value in (
            ("COM_ARM_HFLT_CHK", "0"),
            ("COM_MODE_ARM_CHK", "0"),
            ("CBRK_VTOLARMING", "22027"),
            ("NAV_DLL_ACT", "0"),
        ):
            result = _run(
                docker,
                ["exec", container_name, "/opt/px4-gazebo/bin/px4-param", "set", parameter, value],
                timeout_s=10.0,
                check=True,
            )
            parameter_overrides[parameter] = {
                "value": value,
                "stdout": result.stdout.strip(),
                "scope": "owned disposable combined PX4/Gazebo SITL only",
            }

        collector.send_gcs_heartbeat(force=True)
        arm_result = _send_command(
            collector,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            1.0,
            21196.0,
        )
        command_events.append(
            {
                "command": "MAV_CMD_COMPONENT_ARM_DISARM",
                "result": arm_result,
                "arm": True,
            }
        )
        collector.wait_for_armed_state(True, timeout_s=armed_state_timeout_s)
        phase_events.append({"phase": "multicopter_armed", "at_s": round(collector.elapsed_s, 6)})

        transition_fw = _send_command(
            collector,
            mavutil.mavlink.MAV_CMD_DO_VTOL_TRANSITION,
            float(mavutil.mavlink.MAV_VTOL_STATE_FW),
        )
        command_events.append(
            {
                "command": "MAV_CMD_DO_VTOL_TRANSITION",
                "target": "fixed_wing",
                "result": transition_fw,
            }
        )
        collector.wait_for_vtol_state(
            mavutil.mavlink.MAV_VTOL_STATE_FW,
            transition_timeout_s,
        )
        fw_phase = {
            "phase": "transition_to_fixed_wing",
            "at_s": round(collector.elapsed_s, 6),
            "vtol_state": int(mavutil.mavlink.MAV_VTOL_STATE_FW),
        }
        phase_events.append(fw_phase)
        if fixed_wing_ready_file is not None:
            _write_json(
                fixed_wing_ready_file,
                {
                    **fw_phase,
                    "container_id": container_id,
                    "evaluator_endpoint": endpoint,
                    "product_runtime_flight_control_enabled": False,
                },
            )
        collector.collect(fixed_wing_hold_s)

        transition_mc = _send_command(
            collector,
            mavutil.mavlink.MAV_CMD_DO_VTOL_TRANSITION,
            float(mavutil.mavlink.MAV_VTOL_STATE_MC),
        )
        command_events.append(
            {
                "command": "MAV_CMD_DO_VTOL_TRANSITION",
                "target": "multicopter",
                "result": transition_mc,
            }
        )
        collector.wait_for_vtol_state(
            mavutil.mavlink.MAV_VTOL_STATE_MC,
            transition_timeout_s,
        )
        phase_events.append(
            {
                "phase": "transition_to_multicopter",
                "at_s": round(collector.elapsed_s, 6),
                "vtol_state": int(mavutil.mavlink.MAV_VTOL_STATE_MC),
            }
        )
        collector.collect(multicopter_hold_s)

        disarm_result = _send_command(
            collector,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0.0,
            21196.0,
        )
        command_events.append(
            {
                "command": "MAV_CMD_COMPONENT_ARM_DISARM",
                "result": disarm_result,
                "arm": False,
            }
        )
        # COMMAND_ACK is asynchronous.  PX4 may first settle the land
        # detector and only then emit the unarmed heartbeat, so require a
        # fresh heartbeat with the requested state inside a bounded window.
        collector.wait_for_armed_state(False, timeout_s=12.0)
        _wait_for_landed_disarmed_state(collector)
        phase_events.append({"phase": "completed", "at_s": round(collector.elapsed_s, 6)})

        position_quality = _position_quality(collector)
        if not position_quality["qualified"]:
            raise VtolAcceptanceError(
                "attached VTOL GPS/position quality did not meet the SIM gate"
            )
        if position_quality["false_geolocations"]:
            raise VtolAcceptanceError("attached VTOL stream contained a false geolocation jump")

        fault_events = []
        for fault in fault_timeline:
            event = dict(fault)
            event["observed_or_applied"] = event["effective_at_s"] <= collector.elapsed_s
            event["runtime_effect"] = (
                "camera_input_unavailable"
                if event["kind"] == "camera_dropout"
                else "battery_quality_degraded"
            )
            if event["observed_or_applied"]:
                fault_events.append(event)

        payload: dict[str, Any] = {
            "schema": "multidetect.px4-vtol-attached-combined-sim-acceptance",
            "schema_version": 1,
            "generated_at_utc": _utc_now(),
            "gate_passed": True,
            "failure": None,
            "release_status": RELEASE_STATUS,
            "scenario": {
                "manifest": str(source),
                "manifest_sha256": _sha256_path(source),
                "canonical_manifest_sha256": evaluation_manifest.canonical_hash,
                "scenario_id": evaluation_manifest.scenario_id,
                "scenario_version": evaluation_manifest.scenario_version,
                "seed": runtime_input.seed,
                "truth_loaded_by_runtime": False,
                "expectations_loaded_by_runtime": False,
            },
            "container": {
                "name": container_name,
                "id": container_id,
                "purpose_label": expected_container_label,
                "lifecycle_owner": "combined PowerShell orchestrator",
                "created_by_this_helper": False,
                "stopped_by_this_helper": False,
            },
            "mavlink": {
                "endpoint": endpoint,
                "source_system": source_system,
                "source_component": source_component,
                "message_count": len(collector.messages),
                "command_events": command_events,
                "channel": "test-only combined-SIM evaluator",
            },
            "parameter_overrides": parameter_overrides,
            "timing": {
                "armed_state_timeout_s": armed_state_timeout_s,
                "disarmed_state_timeout_s": 12.0,
                "landed_state_settle_timeout_s": LANDED_STATE_SETTLE_TIMEOUT_SECONDS,
            },
            "phases": phase_events,
            "fault_timeline": fault_timeline,
            "fault_events_observed": fault_events,
            "position_quality": position_quality,
            "safety": {
                "terminal_state": "completed",
                "allowed_terminal_states": list(
                    evaluation_manifest.expectations.allowed_terminal_states
                ),
                "unauthorized_commands": 0,
                "evaluator_command_count": len(command_events),
                "evaluator_command_allowlist": [
                    "MAV_CMD_COMPONENT_ARM_DISARM",
                    "MAV_CMD_DO_VTOL_TRANSITION",
                ],
                "product_runtime_flight_control_enabled": False,
                "product_runtime_actuator_output_enabled": False,
                "payload_control_enabled": False,
                "real_pixhawk_contacted": False,
                "real_jetson_contacted": False,
                "real_camera_contacted": False,
            },
            "qgc": {
                "same_production_qgc_surface": True,
                "operator_visual_acceptance": "not_claimed",
                "machine_visual_smoke": "owned by combined orchestrator",
            },
            "telemetry_tail": collector.messages[-200:],
        }
    except (OSError, RuntimeError, ValueError, VtolAcceptanceError) as exc:
        failure = str(exc)
        payload = {
            "schema": "multidetect.px4-vtol-attached-combined-sim-acceptance",
            "schema_version": 1,
            "generated_at_utc": _utc_now(),
            "gate_passed": False,
            "failure": failure,
            "release_status": RELEASE_STATUS,
            "scenario": {
                "manifest": str(source),
                "manifest_sha256": _sha256_path(source),
                "canonical_manifest_sha256": evaluation_manifest.canonical_hash,
                "scenario_id": evaluation_manifest.scenario_id,
                "truth_loaded_by_runtime": False,
                "expectations_loaded_by_runtime": False,
            },
            "container": {
                "name": container_name,
                "id": container_id,
                "purpose_label": expected_container_label,
                "created_by_this_helper": False,
                "stopped_by_this_helper": False,
            },
            "mavlink": {
                "endpoint": endpoint,
                "source_system": source_system,
                "source_component": source_component,
                "command_events": command_events,
                "message_count": len(collector.messages) if collector is not None else 0,
            },
            "phases": phase_events,
            "fault_timeline": fault_timeline,
            "parameter_overrides": parameter_overrides,
            "timing": {
                "armed_state_timeout_s": armed_state_timeout_s,
                "disarmed_state_timeout_s": 12.0,
                "landed_state_settle_timeout_s": LANDED_STATE_SETTLE_TIMEOUT_SECONDS,
            },
            "safety": {
                "product_runtime_flight_control_enabled": False,
                "real_pixhawk_contacted": False,
                "real_jetson_contacted": False,
            },
            "qgc": {"operator_visual_acceptance": "not_claimed"},
            "telemetry_tail": collector.messages[-200:] if collector is not None else [],
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    _write_json(evidence_path, payload)
    print(json.dumps(payload, sort_keys=True))
    if not payload["gate_passed"]:
        raise SystemExit(1)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--evaluator-port", type=int, required=True)
    parser.add_argument("--expected-container-label", default=EXPECTED_CONTAINER_LABEL)
    parser.add_argument("--fault-time-scale", type=float, default=0.01)
    parser.add_argument("--transition-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--fixed-wing-hold-seconds", type=float, default=8.0)
    parser.add_argument("--multicopter-hold-seconds", type=float, default=1.0)
    parser.add_argument(
        "--armed-state-timeout-seconds",
        type=float,
        default=ARMED_STATE_TIMEOUT_SECONDS,
        help="bounded wait for a fresh PX4 armed heartbeat after COMMAND_ACK",
    )
    parser.add_argument("--fixed-wing-ready-file", type=Path)
    parser.add_argument("--source-system", type=int, default=GCS_SOURCE_SYSTEM)
    parser.add_argument("--source-component", type=int, default=GCS_SOURCE_COMPONENT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    docker = _docker_path()
    run_attached_acceptance(
        args.scenario_manifest,
        args.evidence.resolve(),
        docker=docker,
        container_name=args.container_name,
        evaluator_port=args.evaluator_port,
        expected_container_label=args.expected_container_label,
        fault_time_scale=args.fault_time_scale,
        transition_timeout_s=args.transition_timeout_seconds,
        fixed_wing_hold_s=args.fixed_wing_hold_seconds,
        multicopter_hold_s=args.multicopter_hold_seconds,
        armed_state_timeout_s=args.armed_state_timeout_seconds,
        fixed_wing_ready_file=(
            args.fixed_wing_ready_file.resolve() if args.fixed_wing_ready_file else None
        ),
        source_system=args.source_system,
        source_component=args.source_component,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

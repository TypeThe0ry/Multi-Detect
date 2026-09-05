from __future__ import annotations

import json
from pathlib import Path

from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate
from multidetect.release_gate_manifest import create_release_gate_manifest
from multidetect.release_mode_evidence import (
    build_release_mode_evidence,
    write_release_mode_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = REPO_ROOT / "configs" / "product-version.json"
MODES = ("replay", "sim", "hil", "real")
PARITY = {field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)}


def _mode_evidence(tmp_path: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for mode in MODES:
        path = tmp_path / f"{mode}.json"
        path.write_text(
            json.dumps(
                {
                    "event": f"{mode}_acceptance_finished",
                    "runtime_mode": mode,
                    "gate_passed": True,
                }
            ),
            encoding="utf-8",
        )
        result[mode] = path
    return result


def _combined_document() -> dict[str, object]:
    ports = list(range(30_001, 30_010))
    port_fields = (
        "operator_server_udp",
        "operator_qgc_udp",
        "px4_telemetry_udp",
        "px4_input_host_udp",
        "qgc_px4_udp",
        "qgc_router_udp",
        "evaluator_udp",
        "evaluator_router_udp",
        "rtsp_tcp",
    )
    port_record: dict[str, object] = {
        "allocation_mode": "dynamic_loopback",
        "dynamic_range": {"minimum": 30_000, "maximum": 60_000},
        "allocated_host_ports": ports,
        "protected_udp_14550_before": [],
        "protected_udp_14550_after": [],
        "protected_udp_14550_unchanged": True,
        "all_test_ports_released": True,
    }
    port_record.update(dict(zip(port_fields, ports, strict=True)))
    return {
        "schema": "multidetect.px4-gazebo-qgc-combined-sim-acceptance",
        "schema_version": 2,
        "runtime_mode": "sim",
        "release_status": "v0.3.0-pre.8; final v0.3.0 remains blocked",
        "gate_passed": True,
        "ports": port_record,
        "router": {
            "all_ports_distinct": True,
            "all_udp_endpoints_loopback": True,
            "software_only": True,
            "protected_udp_14550_used": False,
            "real_pixhawk_contacted": False,
            "physical_actuator_outputs_present": False,
            "qgc_flight_commands_allowed": False,
        },
        "docker": {
            "container_started": True,
            "container_stopped_and_removed": True,
            "exactly_one_px4_gazebo_container": True,
        },
        "gazebo_sensor_topics": {"required_ready": True},
        "integrated_sim_qgc": {
            "gate_passed": True,
            "runtime_mode": "sim",
            "hardware_outputs_present": False,
            "real_pixhawk_contacted": False,
            "actuator_outputs_present": False,
            "flight_control_enabled": False,
            "operator_visual_acceptance": "not_claimed",
        },
        "vtol_attached": {"gate_passed": True},
        "vtol_mission": {"gate_passed": True},
        "qgc": {"operator_visual_acceptance": "not_claimed"},
    }


def test_release_gate_rehashes_and_reverifies_combined_sim_binding(tmp_path: Path) -> None:
    evidence = _mode_evidence(tmp_path)
    combined = tmp_path / "combined.json"
    combined.write_text(json.dumps(_combined_document(), indent=2), encoding="utf-8")
    release_mode = tmp_path / "sim-release-mode.json"
    envelope = build_release_mode_evidence(
        combined,
        mode="sim",
        output_path=release_mode,
    )
    write_release_mode_evidence(release_mode, envelope)
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses={
            mode: {
                "operator_visual_acceptance": "passed",
                "mission_path_acceptance": "passed",
                "hardware_acceptance": "not_required" if mode in {"replay", "sim"} else "passed",
                "real_control_acceptance": "not_required"
                if mode in {"replay", "sim"}
                else "passed",
            }
            for mode in MODES
        },
        version_manifest_path=VERSION_MANIFEST,
        simulator_combined_evidence=combined,
        simulator_combined_release_mode_evidence=release_mode,
    )

    binding = document["simulator_combined"]
    assert binding["evidence_path"] == "../combined.json"
    assert binding["release_mode_path"] == "../sim-release-mode.json"
    assert binding["gate_passed"] is True
    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["simulator_combined"]["passed"] is True
    assert report["gate_passed"] is True

    source = json.loads(combined.read_text(encoding="utf-8"))
    source["ports"]["all_test_ports_released"] = False  # type: ignore[index]
    combined.write_text(json.dumps(source), encoding="utf-8")
    changed = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert changed["gate_passed"] is False
    assert changed["simulator_combined"]["passed"] is False
    assert any(
        "simulator_combined evidence SHA-256 does not match" in reason
        for reason in changed["release_blocked_reasons"]
    )

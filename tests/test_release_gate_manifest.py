from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.actuator_safety import run_actuator_safety_software_acceptance
from multidetect.alert_link_bench import verify_alert_link_bench_report
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.field_evidence_bundle import (
    check_field_evidence_bundle,
    write_field_evidence_bundle,
)
from multidetect.field_session_capture import capture_field_session
from multidetect.geolocation_accuracy import (
    GeolocationAccuracySample,
    evaluate_geolocation_accuracy,
)
from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate
from multidetect.release_gate_manifest import (
    ReleaseGateManifestError,
    build_release_gate_manifest,
    create_release_gate_manifest,
    default_mode_statuses,
    load_parity_hashes,
    main,
    sha256_file,
    write_release_gate_manifest,
)
from multidetect.replay_set import build_replay_set_manifest
from multidetect.simulator.safety_evidence import run_acceptance
from multidetect.simulator.scenario_matrix import run_scenario_matrix
from multidetect.simulator.scenario_runner import (
    SimulatorScenarioRunner,
    SimulatorScenarioRunnerConfig,
)
from scripts.run_simulator_3d_control_acceptance import run_acceptance as run_3d_control_acceptance

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = REPO_ROOT / "configs" / "product-version.json"
RELEASE_GATE_SCHEMA = REPO_ROOT / "configs" / "schemas" / "release-gate-manifest.schema.json"
MODES = ("replay", "sim", "hil", "real")
PARITY = {field: f"{index + 1:064x}" for index, field in enumerate(PARITY_FIELDS)}


def _write_evidence(path: Path, mode: str, **extra: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, object] = {
        "event": f"{mode}_acceptance_finished",
        "gate_passed": True,
        "runtime_mode": mode,
    }
    document.update(extra)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _evidence(tmp_path: Path) -> dict[str, Path]:
    return {
        mode: _write_evidence(tmp_path / "evidence" / f"{mode}.json", mode)
        for mode in MODES
    }


def _field_link_report(
    tmp_path: Path,
    *,
    ready: bool = True,
    generated_at_epoch_s: float | None = None,
) -> Path:
    path = tmp_path / "field-link" / ("ready.json" if ready else "unready.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if generated_at_epoch_s is None:
        generated_at_epoch_s = time.time()
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at_epoch_s": generated_at_epoch_s,
                "probes": [{"name": "jetson", "ok": ready}],
                "summary": {"all_passed": ready, "hardware_link_ready": ready},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _real_alert_link_report(tmp_path: Path) -> Path:
    """Write a deterministic hardware-domain report for binding tests.

    The fixture exercises manifest provenance only; no socket or aircraft is
    contacted.  The standalone verifier still recomputes the complete report
    contract before the test binds it.
    """

    path = tmp_path / "alert-link" / "hardware.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 1,
        "event": "alert_link_bench_passed",
        "observed_at_utc": "2026-09-03T10:00:00+00:00",
        "hardware_observed": True,
        "simulation_only": False,
        "passed": True,
        "reasons": [],
        "checks": {
            "required_round_trips": True,
            "packet_loss_limit": True,
            "ack_latency_limit": True,
            "mode_boundary": True,
            "correlated_ack": True,
            "authenticated_transport": True,
        },
        "transport_protocol": "authenticated_udp",
        "remote_host": "192.168.1.5",
        "remote_port": 14600,
        "remote_is_loopback": False,
        "hardware_id": "GR01-ALERT-001",
        "requested_round_trips": 20,
        "round_trip_samples": 20,
        "failed_round_trips": 0,
        "transmission_attempts": 20,
        "missing_ack_attempts": 0,
        "packet_loss_rate": 0.0,
        "ack_latency_p50_ms": 1.0,
        "ack_latency_p95_ms": 2.0,
        "correlated_ack_verified": True,
        "authenticated_ack_verified": True,
        "failure_types": {},
        "thresholds": {
            "maximum_packet_loss_rate": 0.01,
            "maximum_ack_latency_p95_ms": 500.0,
            "maximum_attempts": 3,
        },
        "alert_hardware_control_enabled": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "payload_release_requested": False,
        "production_approved": False,
        "physical_release_approved": False,
    }
    assert verify_alert_link_bench_report(document, require_passed=True)["gate_passed"] is True
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _route_field_link_report(tmp_path: Path) -> Path:
    path = tmp_path / "field-link" / "route-ready.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at_epoch_s": time.time(),
                "probes": [{"name": "jetson", "ok": True}],
                "route_probes": [
                    {
                        "name": "jetson-route",
                        "transport": "route",
                        "host": "192.168.144.20",
                        "port": 22,
                        "ok": True,
                        "source_address": "192.168.144.2",
                    }
                ],
                "summary": {
                    "all_passed": True,
                    "hardware_link_ready": True,
                    "configuration_valid": True,
                    "route_checks_requested": True,
                    "route_checks_passed": True,
                    "route_source_cidr": "192.168.144.0/24",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _partial_watch_route_field_link_report(tmp_path: Path) -> Path:
    path = tmp_path / "field-link" / "partial-watch-route.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "watch",
                "finished_at_epoch_s": time.time(),
                "configuration": {"samples_requested": 3},
                "samples": [
                    {
                        "sample_index": 1,
                        "probes": [{"name": "jetson", "ok": False}],
                        "route_probes": [
                            {
                                "name": "jetson-route",
                                "transport": "route",
                                "host": "192.168.144.20",
                                "port": 22,
                                "ok": False,
                                "source_address": "192.168.1.13",
                            }
                        ],
                        "summary": {
                            "all_passed": False,
                            "hardware_link_ready": False,
                            "configuration_valid": True,
                            "route_checks_requested": True,
                            "route_checks_passed": False,
                        },
                    }
                ],
                "summary": {
                    "samples_requested": 3,
                    "samples_collected": 1,
                    "route_checks_requested": True,
                    "route_checks_passed": None,
                    "route_source_cidr": "192.168.144.0/24",
                    "all_samples_passed": False,
                    "all_passed": False,
                    "hardware_link_ready": False,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _simulator_safety_evidence(tmp_path: Path) -> Path:
    path = tmp_path / "simulator-safety" / "acceptance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            run_acceptance(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _actuator_safety_evidence(tmp_path: Path) -> Path:
    path = tmp_path / "actuator-safety" / "acceptance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            run_actuator_safety_software_acceptance(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _simulator_scenario_matrix_evidence(tmp_path: Path) -> Path:
    path = tmp_path / "simulator-matrix" / "matrix.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = run_scenario_matrix(
        REPO_ROOT / "configs" / "scenarios",
        frames=2,
        dt_s=0.1,
        enable_ai=True,
        arm_on_start=True,
        repeat=True,
    )
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _simulator_3d_control_evidence(tmp_path: Path) -> list[Path]:
    manifests = (
        REPO_ROOT / "configs" / "scenarios" / "coastal_patrol_fixed_wing_001.scenario.json",
        REPO_ROOT / "configs" / "scenarios" / "urban_inspection_multirotor_001.scenario.json",
        REPO_ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json",
    )
    output_paths: list[Path] = []
    for manifest in manifests:
        document = run_3d_control_acceptance(
            manifest,
            launch_job_dir=tmp_path / "simulator-3d-control-launch" / manifest.stem,
        )
        path = tmp_path / "simulator-3d-control" / f"{manifest.stem}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        output_paths.append(path)
    return output_paths


def _simulator_scene_stream_set_evidence(tmp_path: Path) -> list[Path]:
    manifests = (
        REPO_ROOT / "configs" / "scenarios" / "coastal_patrol_fixed_wing_001.scenario.json",
        REPO_ROOT / "configs" / "scenarios" / "urban_inspection_multirotor_001.scenario.json",
        REPO_ROOT / "configs" / "scenarios" / "river_rescue_vtol_001.scenario.json",
    )
    output_paths: list[Path] = []
    for manifest in manifests:
        result = SimulatorScenarioRunner(
            manifest,
            config=SimulatorScenarioRunnerConfig(
                frames=2,
                dt_s=0.1,
                enable_ai=True,
                arm_on_start=True,
                record_scene_stream=True,
            ),
        ).run()
        assert result.scene_stream is not None
        path = tmp_path / "simulator-scene-stream-set" / f"{manifest.stem}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        result.scene_stream.write_json(path)
        output_paths.append(path)
    return output_paths


def _target_accuracy_report(
    tmp_path: Path,
    *,
    maximum_p95_m: float | None = 2.0,
    with_thresholds: bool = True,
) -> Path:
    samples = tuple(
        GeolocationAccuracySample(
            sample_id=f"sample-{index:02d}",
            estimated_latitude_deg=1.3,
            estimated_longitude_deg=103.8 + (1.0 / 111_000.0) * 0.00001 * index,
            ground_truth_latitude_deg=1.3,
            ground_truth_longitude_deg=103.8 + (1.0 / 111_000.0) * 0.00001 * index,
            reported_sigma_m=2.0,
            target_id="target-1",
            captured_at_s=float(index),
        )
        for index in range(10)
    )
    report = evaluate_geolocation_accuracy(
        samples,
        minimum_samples=10,
        maximum_p95_m=maximum_p95_m,
        minimum_one_sigma_coverage=0.9 if with_thresholds else None,
        minimum_two_sigma_coverage=0.99 if with_thresholds else None,
    )
    path = tmp_path / "target-accuracy" / "accuracy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _target_state_capture_report(tmp_path: Path) -> Path:
    source = tmp_path / "target-state" / "live.audit.jsonl"
    output = tmp_path / "target-state" / "session.jsonl"
    report_path = tmp_path / "target-state" / "session.capture.json"
    source.parent.mkdir(parents=True, exist_ok=True)

    def record(state: str, timestamp_s: float) -> dict[str, object]:
        return {
            "sequence": int(timestamp_s * 10) + 1,
            "session_id": "session-a",
            "timestamp_s": timestamp_s,
            "event_type": "field.state_changed",
            "details": {
                "state": state,
                "target_id": "target-a",
                "gps": {
                    "position_healthy": True,
                    "latitude_deg": 22.3001,
                    "longitude_deg": 114.1734,
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

    source.write_text(
        "\n".join(
            json.dumps(record(state, index * 0.1), ensure_ascii=False)
            for index, state in enumerate(("DET", "TRK", "LCK", "TGT"))
        )
        + "\n",
        encoding="utf-8",
    )
    report = capture_field_session(
        source,
        output,
        require_target_state_evidence=True,
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert report["gate_passed"] is True
    return report_path


def _complete_statuses() -> dict[str, dict[str, str]]:
    statuses: dict[str, dict[str, str]] = {}
    for mode in MODES:
        hardware = "not_required" if mode in {"replay", "sim"} else "passed"
        real_control = "not_required" if mode in {"replay", "sim"} else "passed"
        statuses[mode] = {
            "operator_visual_acceptance": "passed",
            "mission_path_acceptance": "passed",
            "hardware_acceptance": hardware,
            "real_control_acceptance": real_control,
        }
    return statuses


def test_build_hashes_evidence_and_uses_manifest_relative_paths(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
    )

    assert document["schema_version"] == 1
    assert document["release_label"] == "0.3.0-pre.8"
    assert all(record["gate_passed"] is True for record in document["modes"].values())
    for mode in MODES:
        record = document["modes"][mode]
        assert record["evidence_path"] == f"../evidence/{mode}.json"
        assert record["evidence_sha256"] == sha256_file(evidence[mode])
        assert record["artifact_event"] == f"{mode}_acceptance_finished"

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["gate_passed"] is True
    assert report["release_allowed"] is False  # canonical product is still prerelease


def test_real_field_link_binding_is_hash_bound_and_rechecked(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    link = _field_link_report(tmp_path)
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        real_field_link_preflight=link,
    )

    binding = document["real_field_link"]
    assert binding["report_path"] == "../field-link/ready.json"
    assert binding["report_sha256"] == sha256_file(link)
    assert binding["report_bytes"] == link.stat().st_size
    assert binding["max_age_s"] == 900.0
    assert binding["report_timestamp_epoch_s"] is not None
    assert binding["hardware_link_ready"] is True
    assert binding["gate_passed"] is True

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["real_field_link"]["present"] is True
    assert report["real_field_link"]["passed"] is True

    link.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "probes": [{"name": "jetson", "ok": False}],
                "summary": {"all_passed": False, "hardware_link_ready": False},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    changed = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert changed["gate_passed"] is False
    assert changed["real_field_link"]["passed"] is False
    assert any(
        "real_field_link report SHA-256 does not match" in reason
        for reason in changed["release_blocked_reasons"]
    )


def test_real_field_link_route_summary_is_bound_and_rechecked(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    link = _route_field_link_report(tmp_path)
    output = tmp_path / "manifests" / "release-gate-route.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        real_field_link_preflight=link,
    )

    binding = document["real_field_link"]
    assert binding["route_checks_requested"] is True
    assert binding["route_checks_passed"] is True
    assert binding["route_source_cidr"] == "192.168.144.0/24"

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["real_field_link"]["passed"] is True
    assert report["real_field_link"]["report"]["route_checks_requested"] is True
    assert report["real_field_link"]["report"]["route_checks_passed"] is True
    assert report["real_field_link"]["report"]["route_source_cidr"] == (
        "192.168.144.0/24"
    )

    tampered = json.loads(output.read_text(encoding="utf-8"))
    tampered["real_field_link"]["route_source_cidr"] = "10.0.0.0/8"
    output.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    changed = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert changed["gate_passed"] is False
    assert any(
        "real_field_link route_source_cidr does not match report" in reason
        for reason in changed["release_blocked_reasons"]
    )


def test_real_field_link_freshness_is_bound_and_rechecked(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    link = _field_link_report(
        tmp_path,
        generated_at_epoch_s=time.time() - 31.0,
    )
    output = tmp_path / "manifests" / "release-gate-stale-field-link.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        real_field_link_preflight=link,
        real_field_link_max_age_s=30.0,
    )

    assert document["real_field_link"]["max_age_s"] == 30.0
    assert document["real_field_link"]["gate_passed"] is False
    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["real_field_link"]["passed"] is False
    assert any(
        "field-link preflight report is stale" in reason
        for reason in report["real_field_link"]["reasons"]
    )


def test_real_field_link_rejects_partial_freshness_binding(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    link = _field_link_report(tmp_path)
    output = tmp_path / "manifests" / "release-gate-partial-freshness.json"

    create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        real_field_link_preflight=link,
    )

    tampered = json.loads(output.read_text(encoding="utf-8"))
    del tampered["real_field_link"]["max_age_s"]
    output.write_text(json.dumps(tampered) + "\n", encoding="utf-8")

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["real_field_link"]["passed"] is False
    assert any(
        "report_timestamp_epoch_s requires max_age_s" in reason
        for reason in report["real_field_link"]["reasons"]
    )


def test_release_gate_accepts_partial_watch_route_null_as_fail_closed(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    link = _partial_watch_route_field_link_report(tmp_path)
    output = tmp_path / "manifests" / "release-gate-partial-watch-route.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        real_field_link_preflight=link,
    )

    assert document["real_field_link"]["route_checks_requested"] is True
    assert document["real_field_link"]["route_checks_passed"] is None

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)

    assert report["real_field_link"]["passed"] is False
    assert report["real_field_link"]["report"]["route_checks_passed"] is None
    assert not any(
        "real_field_link.route_checks_passed must be a boolean when route checks are requested"
        in reason
        for reason in report["real_field_link"]["reasons"]
    )
    assert any(
        "real_field_link: field-link preflight hardware_link_ready is false" in reason
        for reason in report["real_field_link"]["reasons"]
    )


def test_final_release_requires_route_bound_real_field_link(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    link = _field_link_report(tmp_path)
    output = tmp_path / "manifests" / "release-gate-final-route.json"
    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        real_field_link_preflight=link,
    )
    assert document["real_field_link"]["route_checks_requested"] is False
    assert document["real_field_link"]["route_checks_passed"] is None
    assert document["real_field_link"]["route_source_cidr"] is None

    final_version = json.loads(VERSION_MANIFEST.read_text(encoding="utf-8"))
    final_version.update(
        {"release_label": "0.3.0", "python": "0.3.0", "channel": "release"}
    )
    final_version_path = tmp_path / "product-version-release.json"
    final_version_path.write_text(json.dumps(final_version), encoding="utf-8")
    report = evaluate_release_gate(output, version_manifest_path=final_version_path)
    assert any(
        "final release requires route-bound REAL field-link provenance" in reason
        for reason in report["release_blocked_reasons"]
    )

    old_binding = json.loads(output.read_text(encoding="utf-8"))
    del old_binding["real_field_link"]["max_age_s"]
    del old_binding["real_field_link"]["report_timestamp_epoch_s"]
    output.write_text(json.dumps(old_binding) + "\n", encoding="utf-8")
    old_report = evaluate_release_gate(output, version_manifest_path=final_version_path)
    assert any(
        "final release requires freshness-bound REAL field-link provenance" in reason
        for reason in old_report["release_blocked_reasons"]
    )


def test_real_alert_link_binding_is_hash_bound_and_reverified(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    alert_report = _real_alert_link_report(tmp_path)
    output = tmp_path / "manifests" / "release-gate-alert-link.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        real_alert_link_bench_report=alert_report,
    )

    binding = document["real_alert_link"]
    assert binding["report_path"] == "../alert-link/hardware.json"
    assert binding["report_sha256"] == sha256_file(alert_report)
    assert binding["report_bytes"] == alert_report.stat().st_size
    assert binding["event"] == "alert_link_bench_passed"
    assert binding["hardware_observed"] is True
    assert binding["simulation_only"] is False
    assert binding["remote_host"] == "192.168.1.5"
    assert binding["authenticated_ack_verified"] is True
    assert binding["gate_passed"] is True

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["real_alert_link"]["present"] is True
    assert report["real_alert_link"]["passed"] is True
    assert report["real_alert_link"]["report"]["event"] == (
        "alert_link_bench_passed"
    )

    tampered = json.loads(output.read_text(encoding="utf-8"))
    tampered["real_alert_link"]["remote_host"] = "10.0.0.2"
    output.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    changed = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert changed["gate_passed"] is False
    assert any(
        "real_alert_link remote_host does not match report" in reason
        for reason in changed["release_blocked_reasons"]
    )

    alert_report.write_text(
        alert_report.read_text(encoding="utf-8").replace(
            '"ack_latency_p95_ms": 2.0',
            '"ack_latency_p95_ms": 9.0',
        ),
        encoding="utf-8",
    )
    source_changed = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert source_changed["real_alert_link"]["passed"] is False
    assert any(
        "real_alert_link report SHA-256 does not match" in reason
        for reason in source_changed["release_blocked_reasons"]
    )


def test_real_alert_link_writer_rejects_software_baseline(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    report = _real_alert_link_report(tmp_path)
    source = json.loads(report.read_text(encoding="utf-8"))
    source.update(
        {
            "event": "alert_link_software_baseline_bench_passed",
            "hardware_observed": False,
            "simulation_only": True,
            "remote_host": "127.0.0.1",
            "remote_is_loopback": True,
            "hardware_id": None,
            "authenticated_ack_verified": False,
        }
    )
    # The software-domain mutation is intentionally a valid baseline report;
    # the release manifest writer must still keep it out of REAL evidence.
    source["checks"]["authenticated_transport"] = True
    source["transport_protocol"] = "FakeTransport"
    report.write_text(json.dumps(source, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ReleaseGateManifestError, match="real_alert_link bench report"):
        create_release_gate_manifest(
            output_path=tmp_path / "manifests" / "release-gate.json",
            replay_evidence=evidence["replay"],
            sim_evidence=evidence["sim"],
            hil_evidence=evidence["hil"],
            real_evidence=evidence["real"],
            parity=PARITY,
            statuses=_complete_statuses(),
            version_manifest_path=VERSION_MANIFEST,
            real_alert_link_bench_report=report,
        )


def test_final_release_requires_real_alert_link_binding(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    output = tmp_path / "manifests" / "release-gate-final-alert-link.json"
    create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
    )
    final_version = json.loads(VERSION_MANIFEST.read_text(encoding="utf-8"))
    final_version.update(
        {"release_label": "0.3.0", "python": "0.3.0", "channel": "release"}
    )
    final_version_path = tmp_path / "product-version-release.json"
    final_version_path.write_text(json.dumps(final_version), encoding="utf-8")

    report = evaluate_release_gate(output, version_manifest_path=final_version_path)
    assert any(
        "final release requires a hash-bound REAL authenticated alert-link bench" in reason
        for reason in report["release_blocked_reasons"]
    )


def test_real_target_accuracy_binding_is_hash_bound_and_recomputed(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    accuracy = _target_accuracy_report(tmp_path)
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        real_target_accuracy_report=accuracy,
    )

    binding = document["real_target_accuracy"]
    assert binding["report_path"] == "../target-accuracy/accuracy.json"
    assert binding["report_sha256"] == sha256_file(accuracy)
    assert binding["report_bytes"] == accuracy.stat().st_size
    assert binding["evidence_type"] == "multidetect.target-geolocation-accuracy.v1"
    assert binding["gate_passed"] is True
    assert binding["sample_count"] == 10
    assert binding["p95_error_m"] >= 0

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["real_target_accuracy"]["present"] is True
    assert report["real_target_accuracy"]["passed"] is True
    assert report["gate_passed"] is True

    raw = json.loads(output.read_text(encoding="utf-8"))
    raw["real_target_accuracy"]["p95_error_m"] = 999.0
    output.write_text(json.dumps(raw), encoding="utf-8")
    changed_binding = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert changed_binding["gate_passed"] is False
    assert any(
        "real_target_accuracy p95_error_m does not match report" in reason
        for reason in changed_binding["release_blocked_reasons"]
    )

    source = json.loads(accuracy.read_text(encoding="utf-8"))
    source["samples"][0]["estimated_longitude_deg"] += 0.00001
    accuracy.write_text(json.dumps(source), encoding="utf-8")
    changed_source = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert changed_source["real_target_accuracy"]["passed"] is False
    assert any(
        "real_target_accuracy report SHA-256 does not match" in reason
        for reason in changed_source["release_blocked_reasons"]
    )


def test_real_target_accuracy_binding_requires_explicit_threshold(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    accuracy = _target_accuracy_report(
        tmp_path,
        maximum_p95_m=None,
        with_thresholds=False,
    )
    with pytest.raises(
        ReleaseGateManifestError,
        match="explicit accuracy threshold",
    ):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
            real_target_accuracy_report=accuracy,
        )


def test_real_target_state_evidence_binding_is_hash_bound_and_recomputed(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    capture = _target_state_capture_report(tmp_path)
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        real_target_state_evidence_report=capture,
    )

    binding = document["real_target_state_evidence"]
    assert binding["report_path"] == "../target-state/session.capture.json"
    assert binding["report_sha256"] == sha256_file(capture)
    assert binding["report_bytes"] == capture.stat().st_size
    assert binding["schema_version"] == 1
    assert binding["require_target_state_evidence"] is True
    assert binding["gate_passed"] is True
    assert binding["captured_records"] == 4
    assert binding["state_path"] == ["DET", "TRK", "LCK", "TGT"]
    assert binding["gps_qualified_records"] == [3]
    assert binding["range_qualified_records"] == [3]

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["real_target_state_evidence"]["present"] is True
    assert report["real_target_state_evidence"]["passed"] is True
    assert report["gate_passed"] is True

    tampered = json.loads(output.read_text(encoding="utf-8"))
    tampered["real_target_state_evidence"]["state_path"] = ["DET", "TGT"]
    output.write_text(json.dumps(tampered), encoding="utf-8")
    changed = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert changed["gate_passed"] is False
    assert any(
        "real_target_state_evidence state_path does not match report" in reason
        for reason in changed["release_blocked_reasons"]
    )


def test_final_release_requires_target_state_evidence_binding(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    output = tmp_path / "manifests" / "release-gate.json"
    create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
    )
    final_version = json.loads(VERSION_MANIFEST.read_text(encoding="utf-8"))
    final_version.update(
        {"release_label": "0.3.0", "python": "0.3.0", "channel": "release"}
    )
    final_version_path = tmp_path / "product-version-release.json"
    final_version_path.write_text(json.dumps(final_version), encoding="utf-8")

    report = evaluate_release_gate(output, version_manifest_path=final_version_path)
    assert any(
        "final release requires a hash-bound REAL target-state evidence capture" in reason
        for reason in report["release_blocked_reasons"]
    )


def test_simulator_safety_binding_is_hash_bound_and_reverified(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    safety = _simulator_safety_evidence(tmp_path)
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        simulator_safety_evidence=safety,
    )

    binding = document["simulator_safety"]
    assert binding["evidence_path"] == "../simulator-safety/acceptance.json"
    assert binding["evidence_sha256"] == sha256_file(safety)
    assert binding["event"] == "simulator_safety_acceptance_finished"
    assert binding["runtime_mode"] == "sim"
    assert binding["release_label"] == "0.3.0-pre.8"
    assert binding["gate_passed"] is True
    assert binding["verifier_event"] == "simulator_safety_evidence_verified"
    assert binding["verifier_exit_code"] == 0
    assert all(binding["checks"].values())

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["simulator_safety"]["present"] is True
    assert report["simulator_safety"]["passed"] is True
    assert report["gate_passed"] is True

    source = json.loads(safety.read_text(encoding="utf-8"))
    source["mission"]["terminal_state"] = "searching"
    safety.write_text(json.dumps(source), encoding="utf-8")
    changed = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert changed["gate_passed"] is False
    assert changed["simulator_safety"]["passed"] is False
    assert any(
        "simulator_safety evidence SHA-256 does not match" in reason
        for reason in changed["release_blocked_reasons"]
    )


def test_simulator_safety_writer_rejects_failed_verifier(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    safety = _simulator_safety_evidence(tmp_path)
    source = json.loads(safety.read_text(encoding="utf-8"))
    source["checks"]["mission_terminal_consistent"] = False
    source["gate_passed"] = False
    safety.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ReleaseGateManifestError, match="simulator_safety evidence"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
            simulator_safety_evidence=safety,
        )


def test_actuator_safety_binding_is_hash_bound_and_reverified(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    safety = _actuator_safety_evidence(tmp_path)
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        actuator_safety_evidence=safety,
    )

    binding = document["actuator_safety"]
    assert binding["evidence_path"] == "../actuator-safety/acceptance.json"
    assert binding["evidence_sha256"] == sha256_file(safety)
    assert binding["event"] == "actuator_safety_software_acceptance_finished"
    assert binding["runtime_mode"] == "HIL"
    assert binding["release_label"] == "0.3.0-pre.8"
    assert binding["gate_passed"] is True
    assert binding["software_only"] is True
    assert binding["physical_outputs_present"] is False
    assert binding["propulsion_disconnected"] == "not_applicable_in_software_fixture"
    assert binding["verifier_event"] == "actuator_safety_software_evidence_verified"
    assert binding["verifier_exit_code"] == 0
    assert set(binding["checks"]) == {
        "direction_mapping",
        "absolute_limits",
        "cancel_emits_neutral",
        "cancel_clears_target_binding",
        "lost_lock_emits_neutral",
        "lost_lock_restores_entry_mode",
        "watchdog_emits_neutral_once",
        "no_command_after_failsafe",
    }
    assert all(binding["checks"].values())

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["actuator_safety"]["present"] is True
    assert report["actuator_safety"]["passed"] is True
    assert report["actuator_safety"]["verification"]["event"] == (
        "actuator_safety_software_evidence_verified"
    )
    assert report["gate_passed"] is True

    source = json.loads(safety.read_text(encoding="utf-8"))
    source["active_command"]["pwm_us"]["pitch"] = 1500
    safety.write_text(json.dumps(source), encoding="utf-8")
    changed = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert changed["gate_passed"] is False
    assert changed["actuator_safety"]["passed"] is False
    assert any(
        "actuator_safety evidence SHA-256 does not match" in reason
        for reason in changed["release_blocked_reasons"]
    )


def test_actuator_safety_writer_rejects_failed_verifier(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    safety = _actuator_safety_evidence(tmp_path)
    source = json.loads(safety.read_text(encoding="utf-8"))
    source["checks"]["no_command_after_failsafe"] = False
    source["gate_passed"] = False
    safety.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(ReleaseGateManifestError, match="actuator_safety evidence"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
            actuator_safety_evidence=safety,
        )


def test_simulator_scenario_matrix_binding_is_hash_bound_and_reverified(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    matrix = _simulator_scenario_matrix_evidence(tmp_path)
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        simulator_scenario_matrix_evidence=matrix,
    )

    binding = document["simulator_scenario_matrix"]
    assert binding["evidence_path"] == "../simulator-matrix/matrix.json"
    assert binding["evidence_sha256"] == sha256_file(matrix)
    assert binding["evidence_bytes"] == matrix.stat().st_size
    assert binding["canonical_sha256"] == json.loads(matrix.read_text())["sha256"]
    assert binding["scenario_count"] == 3
    assert binding["modes"] == ["fixed-wing", "multirotor", "vtol"]
    assert binding["enable_ai"] is True
    assert binding["arm_on_start"] is True
    assert binding["repeatability_checked"] is True
    assert binding["verifier_event"] == "simulator_scenario_matrix_verified"
    assert binding["verifier_exit_code"] == 0
    assert all(binding["checks"].values())

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["simulator_scenario_matrix"]["present"] is True
    assert report["simulator_scenario_matrix"]["passed"] is True
    assert report["gate_passed"] is True

    raw = json.loads(output.read_text(encoding="utf-8"))
    raw["simulator_scenario_matrix"]["checks"]["embedded_sha256"] = False
    output.write_text(json.dumps(raw), encoding="utf-8")
    tampered_binding = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert tampered_binding["gate_passed"] is False
    assert tampered_binding["simulator_scenario_matrix"]["passed"] is False
    assert any(
        "simulator_scenario_matrix checks do not match verifier output" in reason
        for reason in tampered_binding["release_blocked_reasons"]
    )

    source = json.loads(matrix.read_text(encoding="utf-8"))
    source["scenarios"][0]["completed_frames"] = 999
    matrix.write_text(json.dumps(source), encoding="utf-8")
    tampered_source = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert tampered_source["simulator_scenario_matrix"]["passed"] is False
    assert any(
        "simulator_scenario_matrix evidence SHA-256 does not match" in reason
        for reason in tampered_source["release_blocked_reasons"]
    )


def test_simulator_3d_control_binding_is_hash_bound_and_reverified(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    controls = _simulator_3d_control_evidence(tmp_path)
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        simulator_3d_control_evidence=controls,
    )

    binding = document["simulator_3d_control"]
    assert binding["schema_version"] == 1
    assert binding["modes"] == ["fixed-wing", "multirotor", "vtol"]
    assert binding["scenario_ids"] == [
        "coastal_patrol_fixed_wing_001",
        "river_rescue_vtol_001",
        "urban_inspection_multirotor_001",
    ]
    assert binding["gate_passed"] is True
    assert binding["deterministic_gate_passed"] is True
    assert binding["external_runtime_gate_passed"] is False
    assert binding["external_runtime_acceptance"] == "not_claimed"
    assert all(item["gate_passed"] for item in binding["evidence"])
    assert all(item["external_runtime_gate_passed"] is False for item in binding["evidence"])
    assert all(binding["checks"].values())

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["simulator_3d_control"]["present"] is True
    assert report["simulator_3d_control"]["passed"] is True
    assert report["gate_passed"] is True

    raw = json.loads(output.read_text(encoding="utf-8"))
    raw["simulator_3d_control"]["checks"]["all_airframes"] = False
    output.write_text(json.dumps(raw), encoding="utf-8")
    tampered_binding = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert tampered_binding["gate_passed"] is False
    assert any(
        "simulator_3d_control checks do not match bound evidence" in reason
        for reason in tampered_binding["release_blocked_reasons"]
    )

    source = json.loads(controls[0].read_text(encoding="utf-8"))
    source["control_contract"]["manual"]["vehicle_advanced"] = False
    controls[0].write_text(json.dumps(source), encoding="utf-8")
    tampered_source = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert tampered_source["simulator_3d_control"]["passed"] is False
    assert any(
        "simulator_3d_control.evidence[0] evidence SHA-256 does not match" in reason
        for reason in tampered_source["release_blocked_reasons"]
    )


def test_simulator_3d_control_writer_requires_all_airframes(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    controls = _simulator_3d_control_evidence(tmp_path)
    with pytest.raises(ReleaseGateManifestError, match="exactly three evidence files"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
            simulator_3d_control_evidence=controls[:2],
        )


def test_simulator_3d_control_binding_rejects_external_claim(tmp_path: Path) -> None:
    controls = _simulator_3d_control_evidence(tmp_path)
    raw = json.loads(controls[0].read_text(encoding="utf-8"))
    raw["external_runtime_gate_passed"] = True
    controls[0].write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ReleaseGateManifestError, match="simulator_3d_control evidence verifier"):
        build_release_gate_manifest(
            _evidence(tmp_path),
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
            simulator_3d_control_evidence=controls,
        )


def test_simulator_3d_control_schema_declares_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    assert "simulator_3d_control" in schema["properties"]
    definition = schema["$defs"]["simulator3DControlRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["schema_version"]["const"] == 1
    assert definition["properties"]["modes"]["const"] == [
        "fixed-wing",
        "multirotor",
        "vtol",
    ]
    assert definition["properties"]["external_runtime_gate_passed"]["const"] is False


def test_simulator_scene_stream_set_binding_is_hash_bound_and_reverified(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    streams = _simulator_scene_stream_set_evidence(tmp_path)
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        simulator_scene_stream_set_evidence=streams,
    )

    binding = document["simulator_scene_stream_set"]
    assert binding["event"] == "simulator_scene_stream_set_verified"
    assert binding["runtime_mode"] == "sim"
    assert binding["release_label"] == "0.3.0-pre.8"
    assert binding["modes"] == ["fixed-wing", "multirotor", "vtol"]
    assert len(binding["evidence"]) == 3
    assert all(item["gate_passed"] is True for item in binding["evidence"])
    assert all(binding["checks"].values())
    release_schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(release_schema).validate(document)

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["simulator_scene_stream_set"]["present"] is True
    assert report["simulator_scene_stream_set"]["passed"] is True
    assert report["simulator_scene_stream_set"]["verification"]["event"] == (
        "simulator_scene_stream_set_revalidated"
    )
    assert report["gate_passed"] is True

    raw = json.loads(output.read_text(encoding="utf-8"))
    raw["simulator_scene_stream_set"]["checks"]["all_airframes"] = False
    output.write_text(json.dumps(raw), encoding="utf-8")
    tampered_binding = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert tampered_binding["gate_passed"] is False
    assert tampered_binding["simulator_scene_stream_set"]["passed"] is False
    assert any(
        "simulator_scene_stream_set: checks do not match recomputed source records" in reason
        for reason in tampered_binding["release_blocked_reasons"]
    )

    source = json.loads(streams[0].read_text(encoding="utf-8"))
    source["snapshots"][0]["pose"]["north_m"] += 1.0
    streams[0].write_text(json.dumps(source), encoding="utf-8")
    tampered_source = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert tampered_source["simulator_scene_stream_set"]["passed"] is False
    assert any(
        "simulator_scene_stream_set: evidence[0] verifier failed" in reason
        for reason in tampered_source["release_blocked_reasons"]
    )


def test_simulator_scene_stream_set_writer_requires_all_airframes(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    streams = _simulator_scene_stream_set_evidence(tmp_path)
    with pytest.raises(ReleaseGateManifestError, match="exactly three evidence files"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
            simulator_scene_stream_set_evidence=streams[:2],
        )


def test_simulator_scene_stream_set_schema_declares_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    assert "simulator_scene_stream_set" in schema["properties"]
    definition = schema["$defs"]["simulatorSceneStreamSetRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["event"]["const"] == (
        "simulator_scene_stream_set_verified"
    )
    assert definition["properties"]["modes"]["const"] == [
        "fixed-wing",
        "multirotor",
        "vtol",
    ]


def test_simulator_scenario_matrix_writer_rejects_no_arm_matrix(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    path = tmp_path / "simulator-matrix" / "no-arm.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = run_scenario_matrix(
        REPO_ROOT / "configs" / "scenarios",
        frames=1,
        arm_on_start=False,
    )
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ReleaseGateManifestError, match="simulator_scenario_matrix evidence"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
            simulator_scenario_matrix_evidence=path,
        )


def test_final_matrix_writer_rejects_nominal_only_fault_coverage(tmp_path: Path) -> None:
    """The final version writer cannot bind a pre-coverage nominal matrix."""

    evidence = _evidence(tmp_path / "final")
    matrix = _simulator_scenario_matrix_evidence(tmp_path / "final")
    source = json.loads(matrix.read_text(encoding="utf-8"))
    source["release_label"] = "0.3.0"
    source.pop("sha256", None)
    source["sha256"] = canonical_sha256(source)
    matrix.write_text(
        json.dumps(source, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    release_version = tmp_path / "final" / "product-version-release.json"
    release_version.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "Multi-Detect",
                "release_label": "0.3.0",
                "numeric": "0.3.0",
                "python": "0.3.0",
                "channel": "release",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ReleaseGateManifestError,
        match="final release requires fault_coverage_required=true",
    ):
        create_release_gate_manifest(
            replay_evidence=evidence["replay"],
            sim_evidence=evidence["sim"],
            hil_evidence=evidence["hil"],
            real_evidence=evidence["real"],
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "final" / "release-gate.json",
            version_manifest_path=release_version,
            simulator_scenario_matrix_evidence=matrix,
        )


def test_release_gate_binds_pending_replay_set_and_keeps_it_closed(tmp_path: Path) -> None:
    replay_root = tmp_path / "replay-set"
    replay_root.mkdir()
    replay_manifest = replay_root / "replay-set.manifest.json"
    build_replay_set_manifest(
        replay_root,
        replay_manifest,
        dataset_id="pending-replay",
        dataset_version="v1",
        allow_incomplete=True,
    )

    evidence = _evidence(tmp_path / "bound")
    output = tmp_path / "bound" / "release-gate.json"
    document = build_release_gate_manifest(
        evidence,
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        replay_set_manifest=replay_manifest,
    )
    write_release_gate_manifest(output, document)

    binding = document["replay_set"]
    assert binding["manifest_path"] == "../replay-set/replay-set.manifest.json"
    assert binding["manifest_sha256"] == sha256_file(replay_manifest)
    assert binding["gate_passed"] is False
    assert binding["accuracy_validated"] is False

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["replay_set"]["present"] is True
    assert report["replay_set"]["passed"] is False
    assert report["gate_passed"] is False
    assert any("replay_set" in reason for reason in report["release_blocked_reasons"])


def test_final_release_requires_replay_set_binding(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path / "final")
    manifest = tmp_path / "final" / "release-gate.json"
    create_release_gate_manifest(
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=manifest,
        version_manifest_path=VERSION_MANIFEST,
    )
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["release_label"] = "0.3.0"
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    release_version = tmp_path / "product-version-release.json"
    release_version.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "Multi-Detect",
                "release_label": "0.3.0",
                "numeric": "0.3.0",
                "python": "0.3.0",
                "channel": "release",
            }
        ),
        encoding="utf-8",
    )
    report = evaluate_release_gate(manifest, version_manifest_path=release_version)
    assert report["gate_passed"] is True
    assert report["release_allowed"] is False
    assert any("final release requires a complete deployment-domain replay_set" in reason
               for reason in report["release_blocked_reasons"])
    assert any("final release requires a hash-bound REAL field-link preflight" in reason
               for reason in report["release_blocked_reasons"])
    assert any("final release requires a hash-bound SIM S4 safety evidence" in reason
               for reason in report["release_blocked_reasons"])
    assert any(
        "final release requires a hash-bound REAL target geolocation accuracy report" in reason
        for reason in report["release_blocked_reasons"]
    )


def test_omitted_statuses_are_fail_closed() -> None:
    assert default_mode_statuses("replay")["operator_visual_acceptance"] == "not_claimed"
    assert default_mode_statuses("replay")["hardware_acceptance"] == "not_required"
    assert default_mode_statuses("hil")["hardware_acceptance"] == "not_claimed"


def test_build_defaults_keep_every_mode_gate_closed(tmp_path: Path) -> None:
    document = build_release_gate_manifest(
        _evidence(tmp_path),
        parity=PARITY,
        output_path=tmp_path / "release-gate.json",
        version_manifest_path=VERSION_MANIFEST,
    )

    assert all(record["gate_passed"] is False for record in document["modes"].values())
    assert all(
        record["operator_visual_acceptance"] == "not_claimed"
        for record in document["modes"].values()
    )
    assert document["modes"]["hil"]["real_control_acceptance"] == "not_claimed"


def test_failed_evidence_is_recorded_without_being_promoted(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    evidence["sim"] = _write_evidence(
        tmp_path / "evidence" / "sim-failed.json", "sim", gate_passed=False
    )
    document = build_release_gate_manifest(
        evidence,
        parity=PARITY,
        statuses=_complete_statuses(),
        output_path=tmp_path / "release-gate.json",
        version_manifest_path=VERSION_MANIFEST,
    )
    assert document["modes"]["sim"]["gate_passed"] is False


def test_parity_json_and_cli_aliases_create_manifest(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "cli" / "release-gate.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
    ]
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["modes"]["real"]["parity"] == PARITY


def test_cli_binds_real_field_link_preflight(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    link = _field_link_report(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "cli" / "release-gate.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
        "--real-field-link-preflight",
        str(link),
    ]
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["real_field_link"]["gate_passed"] is True


def test_cli_binds_real_alert_link_bench_report(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    alert_report = _real_alert_link_report(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "cli" / "release-gate.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
        "--real-alert-link-bench",
        str(alert_report),
    ]
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["real_alert_link"]["gate_passed"] is True
    assert raw["real_alert_link"]["event"] == "alert_link_bench_passed"


def test_cli_binds_real_target_accuracy_report(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    accuracy = _target_accuracy_report(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "cli" / "release-gate.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
        "--real-target-accuracy-report",
        str(accuracy),
    ]
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["real_target_accuracy"]["gate_passed"] is True
    assert raw["real_target_accuracy"]["sample_count"] == 10


def test_cli_binds_real_target_state_evidence_report(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    capture = _target_state_capture_report(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "cli" / "release-gate.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
        "--real-target-state-evidence-report",
        str(capture),
    ]
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["real_target_state_evidence"]["gate_passed"] is True
    assert raw["real_target_state_evidence"]["require_target_state_evidence"] is True


def test_cli_binds_simulator_safety_evidence(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    safety = _simulator_safety_evidence(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "cli" / "release-gate.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
        "--simulator-safety-evidence",
        str(safety),
    ]
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["simulator_safety"]["gate_passed"] is True
    assert raw["simulator_safety"]["verifier_exit_code"] == 0


def test_cli_binds_actuator_safety_evidence(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    safety = _actuator_safety_evidence(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "cli" / "release-gate.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
        "--actuator-safety-evidence",
        str(safety),
    ]
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["actuator_safety"]["gate_passed"] is True
    assert raw["actuator_safety"]["verifier_exit_code"] == 0


def test_cli_binds_simulator_scenario_matrix_evidence(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    matrix = _simulator_scenario_matrix_evidence(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "cli" / "release-gate.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
        "--simulator-scenario-matrix-evidence",
        str(matrix),
    ]
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["simulator_scenario_matrix"]["gate_passed"] is True


def test_cli_binds_simulator_scene_stream_set_evidence(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    streams = _simulator_scene_stream_set_evidence(tmp_path)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "cli" / "release-gate.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
    ]
    for stream in streams:
        arguments.extend(["--simulator-scene-stream-set-evidence", str(stream)])
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["simulator_scene_stream_set"]["gate_passed"] is True
    assert raw["simulator_scene_stream_set"]["modes"] == [
        "fixed-wing",
        "multirotor",
        "vtol",
    ]


def test_cli_unready_real_field_link_requires_allow_incomplete(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    link = _field_link_report(tmp_path, ready=False)
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps({"parity": PARITY}), encoding="utf-8")
    output = tmp_path / "cli" / "release-gate-unready.json"
    arguments = [
        "--out",
        str(output),
        "--parity-json",
        str(parity_file),
        "--real-field-link-preflight",
        str(link),
    ]
    for mode in MODES:
        arguments.extend(
            [
                f"--{mode}",
                str(evidence[mode]),
                f"--{mode}-visual",
                "passed",
                f"--{mode}-mission",
                "passed",
                f"--{mode}-hardware",
                "not_required" if mode in {"replay", "sim"} else "passed",
                f"--{mode}-real-control",
                "not_required" if mode in {"replay", "sim"} else "passed",
            ]
        )
    assert main(arguments) == 2
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["real_field_link"]["hardware_link_ready"] is False
    assert raw["real_field_link"]["gate_passed"] is False
    assert main(arguments + ["--allow-incomplete"]) == 0


def test_release_gate_schema_declares_real_field_link_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    assert "real_field_link" in schema["properties"]
    definition = schema["$defs"]["realFieldLinkRecord"]
    assert definition["additionalProperties"] is False
    assert definition["required"] == [
        "report_path",
        "report_sha256",
        "report_bytes",
        "hardware_link_ready",
        "gate_passed",
    ]
    assert definition["properties"]["route_checks_requested"]["type"] == "boolean"
    assert definition["properties"]["route_checks_passed"]["type"] == ["boolean", "null"]
    assert definition["properties"]["route_source_cidr"]["type"] == ["string", "null"]
    assert definition["properties"]["max_age_s"]["minimum"] == 0
    assert definition["properties"]["report_timestamp_epoch_s"]["type"] == [
        "number",
        "null",
    ]
    assert definition["dependentRequired"] == {
        "max_age_s": ["report_timestamp_epoch_s"],
        "report_timestamp_epoch_s": ["max_age_s"],
    }


def test_release_gate_schema_declares_real_alert_link_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    assert "real_alert_link" in schema["properties"]
    definition = schema["$defs"]["realAlertLinkRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["event"]["const"] == "alert_link_bench_passed"
    assert definition["properties"]["hardware_observed"]["const"] is True
    assert definition["properties"]["simulation_only"]["const"] is False
    assert definition["properties"]["remote_is_loopback"]["const"] is False
    assert definition["properties"]["authenticated_ack_verified"]["const"] is True
    assert set(definition["properties"]["checks"]["required"]) == {
        "required_round_trips",
        "packet_loss_limit",
        "ack_latency_limit",
        "mode_boundary",
        "correlated_ack",
        "authenticated_transport",
    }


def test_release_gate_schema_declares_replay_set_calibration_verification_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    definition = schema["$defs"]["replaySetCalibrationVerification"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["event"]["const"] == (
        "replay_set_threshold_calibration_verified"
    )
    assert definition["properties"]["schema_version"]["const"] == 1
    assert definition["properties"]["gate_passed"]["const"] is True
    assert "report_promotion_ready" in definition["required"]
    assert "recomputed_promotion_ready" in definition["required"]


def test_release_gate_schema_allows_pending_prerelease_mode_statuses() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    definition = schema["$defs"]["modeRecord"]
    assert definition["properties"]["operator_visual_acceptance"] == {
        "$ref": "#/$defs/status"
    }
    assert definition["properties"]["mission_path_acceptance"] == {
        "$ref": "#/$defs/status"
    }


def test_release_gate_schema_declares_real_target_accuracy_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    assert "real_target_accuracy" in schema["properties"]
    definition = schema["$defs"]["realTargetAccuracyRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["evidence_type"]["const"] == (
        "multidetect.target-geolocation-accuracy.v1"
    )
    assert definition["properties"]["ground_truth_isolated"]["const"] is True
    assert definition["properties"]["contains_flight_control"]["const"] is False
    assert "sample_count" in definition["required"]
    assert "p95_error_m" in definition["required"]


def test_release_gate_schema_declares_real_target_state_evidence_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    assert "real_target_state_evidence" in schema["properties"]
    definition = schema["$defs"]["realTargetStateEvidenceRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["schema_version"]["const"] == 1
    assert definition["properties"]["require_target_state_evidence"]["const"] is True
    assert definition["properties"]["state_path"]["items"]["type"] == "string"
    assert "gps_qualified_records" in definition["required"]
    assert "range_qualified_records" in definition["required"]


def test_release_gate_schema_declares_simulator_safety_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    assert "simulator_safety" in schema["properties"]
    definition = schema["$defs"]["simulatorSafetyRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["event"]["const"] == (
        "simulator_safety_acceptance_finished"
    )
    assert definition["properties"]["runtime_mode"]["const"] == "sim"
    assert definition["properties"]["gate_passed"]["const"] is True
    assert definition["properties"]["verifier_exit_code"]["const"] == 0
    assert definition["required"] == [
        "evidence_path",
        "evidence_sha256",
        "event",
        "runtime_mode",
        "release_label",
        "gate_passed",
        "verifier_event",
        "verifier_exit_code",
        "checks",
    ]


def test_release_gate_schema_declares_actuator_safety_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    assert "actuator_safety" in schema["properties"]
    definition = schema["$defs"]["actuatorSafetyRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["event"]["const"] == (
        "actuator_safety_software_acceptance_finished"
    )
    assert definition["properties"]["runtime_mode"]["const"] == "HIL"
    assert definition["properties"]["software_only"]["const"] is True
    assert definition["properties"]["physical_outputs_present"]["const"] is False
    assert definition["properties"]["verifier_exit_code"]["const"] == 0
    assert definition["required"] == [
        "evidence_path",
        "evidence_sha256",
        "event",
        "runtime_mode",
        "release_label",
        "gate_passed",
        "software_only",
        "physical_outputs_present",
        "propulsion_disconnected",
        "verifier_event",
        "verifier_exit_code",
        "checks",
    ]
    assert definition["properties"]["checks"]["required"] == [
        "direction_mapping",
        "absolute_limits",
        "cancel_emits_neutral",
        "cancel_clears_target_binding",
        "lost_lock_emits_neutral",
        "lost_lock_restores_entry_mode",
        "watchdog_emits_neutral_once",
        "no_command_after_failsafe",
    ]


def test_release_gate_schema_declares_simulator_scenario_matrix_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    assert "simulator_scenario_matrix" in schema["properties"]
    definition = schema["$defs"]["simulatorScenarioMatrixRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["event"]["const"] == (
        "simulator_scenario_matrix_finished"
    )
    assert definition["properties"]["scenario_count"]["const"] == 3
    assert definition["properties"]["enable_ai"]["const"] is True
    assert definition["properties"]["arm_on_start"]["const"] is True
    assert definition["properties"]["repeatability_checked"]["const"] is True
    assert definition["properties"]["truth_loaded"]["const"] is False
    assert definition["properties"]["truth_free"]["const"] is True
    assert definition["properties"]["verifier_event"]["const"] == (
        "simulator_scenario_matrix_verified"
    )


def test_cli_repeatable_status_spec_is_explicit_and_conflict_safe(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    output = tmp_path / "status-spec.json"
    arguments = ["--out", str(output), "--parity-json", str(tmp_path / "parity.json")]
    (tmp_path / "parity.json").write_text(json.dumps(PARITY), encoding="utf-8")
    for mode in MODES:
        arguments.extend([f"--{mode}-evidence", str(evidence[mode])])
        arguments.extend(
            [
                "--status",
                f"{mode}.visual=passed",
                "--status",
                f"{mode}.mission=passed",
            ]
        )
        if mode in {"hil", "real"}:
            arguments.extend(
                [
                    "--status",
                    f"{mode}.hardware=passed",
                    "--status",
                    f"{mode}.real-control=passed",
                ]
            )
    assert main(arguments) == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert all(raw["modes"][mode]["gate_passed"] for mode in MODES)

    conflicting = arguments + ["--real-visual", "not_claimed"]
    assert main(conflicting) == 2


def test_cli_writes_incomplete_manifest_but_returns_fail_closed_code(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    output = tmp_path / "release-gate.json"
    arguments = ["--out", str(output)]
    for mode in MODES:
        arguments.extend([f"--{mode}-evidence", str(evidence[mode])])
    for field, value in PARITY.items():
        arguments.extend([f"--{field.replace('_', '-')}", value])

    assert main(arguments) == 2
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["modes"]["replay"]["gate_passed"] is False


def test_fixture_and_goal_snapshot_markers_are_rejected(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    evidence["sim"] = _write_evidence(
        tmp_path / "evidence" / "sim-fixture.json", "sim", fixture=True
    )
    with pytest.raises(ReleaseGateManifestError, match="fixtures"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
        )

    evidence["sim"] = _write_evidence(
        tmp_path / "evidence" / "sim-goal.json",
        "sim",
        goal_snapshot=True,
    )
    with pytest.raises(ReleaseGateManifestError, match="goal snapshots"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
        )


def test_goal_snapshot_without_runtime_identity_is_rejected(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    goal = tmp_path / "goal-snapshot.json"
    goal.write_text(json.dumps({"software_status": "pass_with_environment_gap"}), encoding="utf-8")
    evidence["real"] = goal
    with pytest.raises(ReleaseGateManifestError, match="runtime_mode"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
        )


def test_invalid_mode_identity_and_duplicate_evidence_fail_closed(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    evidence["sim"] = evidence["replay"]
    with pytest.raises(ReleaseGateManifestError, match="reused"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
        )

    evidence = _evidence(tmp_path / "mismatch")
    _write_evidence(evidence["sim"], "replay")
    with pytest.raises(ReleaseGateManifestError, match="expected 'sim'"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "mismatch.json",
            version_manifest_path=VERSION_MANIFEST,
        )


def test_release_gate_manifest_rejects_ambiguous_and_nonfinite_evidence_json(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path / "duplicate")
    evidence["sim"].write_text(
        '{"event":"sim_acceptance_finished","gate_passed":true,'
        '"gate_passed":false,"runtime_mode":"sim"}',
        encoding="utf-8",
    )
    with pytest.raises(ReleaseGateManifestError, match="duplicate JSON key 'gate_passed'"):
        build_release_gate_manifest(
            evidence,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "duplicate" / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
        )

    nonfinite = _evidence(tmp_path / "nonfinite")
    nonfinite["sim"].write_text(
        '{"event":"sim_acceptance_finished","gate_passed":NaN,"runtime_mode":"sim"}',
        encoding="utf-8",
    )
    with pytest.raises(ReleaseGateManifestError, match="non-finite JSON number NaN"):
        build_release_gate_manifest(
            nonfinite,
            parity=PARITY,
            statuses=_complete_statuses(),
            output_path=tmp_path / "nonfinite" / "release-gate.json",
            version_manifest_path=VERSION_MANIFEST,
        )


def test_field_evidence_bundle_binding_rechecks_serialized_report(
    tmp_path: Path,
) -> None:
    evidence = _evidence(tmp_path)
    bundle_path = tmp_path / "field-evidence" / "pending.json"
    write_field_evidence_bundle(bundle_path, check_field_evidence_bundle())
    output = tmp_path / "manifests" / "release-gate.json"

    document = create_release_gate_manifest(
        output_path=output,
        replay_evidence=evidence["replay"],
        sim_evidence=evidence["sim"],
        hil_evidence=evidence["hil"],
        real_evidence=evidence["real"],
        parity=PARITY,
        statuses=_complete_statuses(),
        version_manifest_path=VERSION_MANIFEST,
        field_evidence_bundle=bundle_path,
    )
    binding = document["field_evidence_bundle"]
    assert binding["report_path"] == "../field-evidence/pending.json"
    assert binding["report_sha256"] == sha256_file(bundle_path)
    assert binding["gate_passed"] is False
    assert binding["real_domain_required"] is True
    assert all(value is False for value in binding["strata"].values())

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["field_evidence_bundle"]["present"] is True
    assert report["field_evidence_bundle"]["passed"] is False
    assert report["gate_passed"] is False
    assert any(
        "field_evidence_bundle gate_passed is false" in reason
        for reason in report["release_blocked_reasons"]
    )

    tampered = json.loads(output.read_text(encoding="utf-8"))
    tampered["field_evidence_bundle"]["all_inputs_present"] = True
    output.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    changed = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert changed["field_evidence_bundle"]["passed"] is False
    assert any(
        "field_evidence_bundle all_inputs_present does not match report" in reason
        for reason in changed["release_blocked_reasons"]
    )


def test_release_gate_schema_declares_field_evidence_bundle_binding() -> None:
    schema = json.loads(RELEASE_GATE_SCHEMA.read_text(encoding="utf-8"))
    assert "field_evidence_bundle" in schema["properties"]
    definition = schema["$defs"]["fieldEvidenceBundleRecord"]
    assert definition["additionalProperties"] is False
    assert definition["properties"]["evidence_type"]["const"] == (
        "multidetect.field-evidence-bundle.v1"
    )
    assert definition["properties"]["real_domain_required"]["const"] is True
    assert set(definition["properties"]["strata"]["required"]) == {
        "field_link",
        "gnss",
        "execution",
        "target_event",
        "media_timeline",
        "accuracy",
    }


def test_parity_loader_and_atomic_no_overwrite(tmp_path: Path) -> None:
    parity_file = tmp_path / "parity.json"
    parity_file.write_text(json.dumps(PARITY), encoding="utf-8")
    assert load_parity_hashes(parity_file) == PARITY

    destination = tmp_path / "manifest.json"
    write_release_gate_manifest(destination, {"schema_version": 1})
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == sha256_file(destination)
    with pytest.raises(ReleaseGateManifestError, match="already exists"):
        write_release_gate_manifest(destination, {"schema_version": 2}, overwrite=False)
    assert not list(tmp_path.glob(".manifest.json.*.tmp"))


@pytest.mark.parametrize(
    ("script", "needle"),
    (
        ("scripts/create_release_gate_manifest.py", "--replay-evidence"),
        (
            "scripts/create_release_mode_evidence.py",
            "--allow-missing-source-runtime-mode",
        ),
        ("scripts/check_release_gate.py", "four-mode release gate"),
    ),
)
def test_cli_wrappers_run_directly_without_pythonpath(script: str, needle: str) -> None:
    """A clean checkout can invoke either wrapper before package installation."""

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    # Keep site-packages available for optional runtime imports (the package's
    # public ``__init__`` imports a few integrations), while removing the
    # caller-provided source path.  In a clean checkout with no editable
    # install, the wrapper's repository-src fallback supplies ``multidetect``.
    completed = subprocess.run(
        [sys.executable, script, "--help"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert needle in completed.stdout

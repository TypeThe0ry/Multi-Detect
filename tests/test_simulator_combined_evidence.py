from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from multidetect.mission_journal import (
    DurableMissionJournal,
    MissionJournalIdentity,
    MissionTransactionState,
)
from multidetect.px4_mission import MissionTransactionBindings
from multidetect.release_mode_evidence import (
    build_release_mode_evidence,
    write_release_mode_evidence,
)
from multidetect.simulator_combined_evidence import main, verify_file
from multidetect.target_evidence_card import TargetEvidenceCard


def _combined_document() -> dict[str, object]:
    allocated = list(range(30_001, 30_010))
    port_names = (
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
    ports: dict[str, object] = {
        "allocation_mode": "dynamic_loopback",
        "dynamic_range": {"minimum": 30_000, "maximum": 60_000},
        "allocated_host_ports": allocated,
        "protected_udp_14550_before": [],
        "protected_udp_14550_after": [],
        "protected_udp_14550_unchanged": True,
        "all_test_ports_released": True,
    }
    ports.update(dict(zip(port_names, allocated, strict=True)))
    run_identity_hash = "a" * 64
    card = TargetEvidenceCard(
        card_id="live-frame-12-card",
        event_id="live-frame-12",
        frame_id="frame-12",
        target_id="target-a",
        target_label="person",
        captured_at_s=12.0,
        available=True,
        unavailable_reason=None,
        latitude_deg=31.2,
        longitude_deg=121.5,
        horizontal_sigma_m=2.0,
        gps_status="qualified",
        gps_freshness="fresh",
        gps_fix_type=3,
        gps_satellites_visible=12,
        gps_horizontal_accuracy_m=1.5,
        gps_vertical_accuracy_m=2.5,
        range_validity="VALID",
        range_m=40.0,
        relative_bearing_deg=3.0,
        source_frame_id="frame-12",
        run_identity_sha256=run_identity_hash,
    ).to_mapping()
    return {
        "schema": "multidetect.px4-gazebo-qgc-combined-sim-acceptance",
        "schema_version": 2,
        "runtime_mode": "sim",
        "release_status": "v0.3.0-pre.8; final v0.3.0 remains blocked",
        "gate_passed": True,
        "ports": ports,
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
            "run_identity_hash": run_identity_hash,
            "target_geolocation_acceptance": {
                "configured": True,
                "gate_passed": True,
                "available_geolocation_count": 1,
                "target_evidence_card_count": 1,
                "target_evidence_cards": [card],
                "target_evidence_card_error_count": 0,
                "target_evidence_card_errors": [],
                "target_evidence_cards_read_only": True,
            },
            "sensor_transport": {
                "camera_info_intrinsics_semantic_validation": (
                    "validated_k_p_rectification_plumb_bob_distortion"
                ),
                "complete": True,
                "expected_frame_count": 3,
                "received_frame_count": 3,
                "odometry_required": True,
                "odometry_timestamp_frame_count": 3,
                "full_odometry_pose_velocity_fusion_claimed": True,
            },
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


def _write_evidence_with_envelope(tmp_path: Path) -> tuple[Path, Path]:
    evidence_path = tmp_path / "combined-evidence.json"
    evidence_path.write_text(
        json.dumps(_combined_document(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    envelope_path = tmp_path / "release-mode-sim.json"
    envelope = build_release_mode_evidence(
        evidence_path,
        mode="sim",
        output_path=envelope_path,
        notes="test envelope",
    )
    write_release_mode_evidence(envelope_path, envelope)
    return evidence_path, envelope_path


def _write_bound_mission_journal(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    def digest(char: str) -> str:
        return char * 64

    bindings = MissionTransactionBindings(
        prompt_hash=digest("a"),
        task_spec_hash=digest("b"),
        plan_hash=digest("c"),
        context_snapshot_hash=digest("d"),
        airframe_profile_hash=digest("e"),
        geofence_hash=digest("f"),
        vehicle_identity_hash=digest("1"),
        runtime_descriptor_hash=digest("2"),
        runtime_provenance_hash=digest("3"),
    )
    identity = MissionJournalIdentity(
        transaction_id="tx-combined-journal",
        executable_mission_hash=digest("4"),
        protocol_hash=digest("5"),
        validation_report_hash=digest("6"),
        bindings=bindings,
        mission_item_count=3,
        airframe_adapter_id="px4-vtol-mission-int-v1",
    )
    now = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    path = tmp_path / "vtol-mission.transaction.jsonl"
    journal = DurableMissionJournal.create(
        path, identity, journal_id="combined-journal", now_utc=now
    )
    journal.record_transition(
        MissionTransactionState.UPLOADING,
        payload={"protocol_hash": digest("5"), "item_count": 3},
        now_utc=now,
    )
    for sequence in range(3):
        journal.record_item_request(sequence, now_utc=now)
    journal.record_transition(
        MissionTransactionState.UPLOADED,
        payload={"ack_type": 0, "mission_type": 0, "source_system": 1, "source_component": 1},
        now_utc=now,
    )
    journal.record_transition(
        MissionTransactionState.READBACK_VERIFIED,
        payload={"protocol_hash": digest("5"), "item_count": 3},
        now_utc=now,
    )
    journal.record_transition(
        MissionTransactionState.COMMITTED,
        payload={"protocol_hash": digest("5")},
        now_utc=now,
    )
    journal.record_transition(
        MissionTransactionState.START_PERMITTED,
        payload={
            "transaction_id": identity.transaction_id,
            "executable_mission_hash": digest("4"),
            "protocol_hash": digest("5"),
        },
        now_utc=now,
    )
    summary = {
        "path": str(path),
        "recovery_required": False,
        "state": "start_permitted",
        "record_count": len(journal.records),
        "requested_sequences": [0, 1, 2],
        "identity_hash": identity.canonical_hash,
    }
    journal.close()
    return path, summary


def test_combined_sim_verifier_binds_dynamic_ports_and_e7_envelope(tmp_path: Path) -> None:
    evidence_path, envelope_path = _write_evidence_with_envelope(tmp_path)

    result = verify_file(
        evidence_path,
        expected_release_label="0.3.0-pre.8",
        release_mode_evidence_path=envelope_path,
        require_release_mode=True,
    )

    assert result["gate_passed"] is True
    assert result["checks"]["ports_count"] is True
    assert result["checks"]["ports_unique"] is True
    assert result["checks"]["protected_port_unchanged"] is True
    assert result["checks"]["test_ports_released"] is True
    assert result["checks"]["release_mode_source_hash_matches"] is True
    assert result["checks"]["camera_info_intrinsics_semantics_validated"] is True
    assert result["checks"]["sensor_frames_complete"] is True
    assert result["checks"]["odometry_frames_complete"] is True
    assert result["checks"]["no_hardware_claims"] is True
    assert result["checks"]["no_operator_visual_claim"] is True


def test_combined_sim_verifier_validates_bound_mission_journal(tmp_path: Path) -> None:
    journal_path, summary = _write_bound_mission_journal(tmp_path)
    document = _combined_document()
    document["vtol_mission"] = {
        "gate_passed": True,
        "protocol": {"mission_item_count": 3},
        "mission_journal": summary,
    }
    document["vtol_mission_journal"] = summary
    document["hashes"] = {
        "mission_journal": {
            "path": str(journal_path),
            "bytes": journal_path.stat().st_size,
            "sha256": hashlib.sha256(journal_path.read_bytes()).hexdigest(),
        }
    }
    document["artifacts"] = {"mission_journal": str(journal_path)}
    evidence_path = tmp_path / "combined-evidence.json"
    evidence_path.write_text(json.dumps(document), encoding="utf-8")
    envelope_path = tmp_path / "release-mode-sim.json"
    envelope = build_release_mode_evidence(
        evidence_path,
        mode="sim",
        output_path=envelope_path,
        notes="journal binding test",
    )
    write_release_mode_evidence(envelope_path, envelope)

    result = verify_file(
        evidence_path,
        expected_release_label="0.3.0-pre.8",
        release_mode_evidence_path=envelope_path,
        require_release_mode=True,
    )

    assert result["gate_passed"] is True
    assert result["checks"]["mission_journal_present"] is True
    assert result["checks"]["mission_journal_path_bound"] is True
    assert result["checks"]["mission_journal_hash_matches"] is True
    assert result["checks"]["mission_journal_integrity"] is True
    assert result["checks"]["mission_journal_summary_matches"] is True
    assert result["checks"]["mission_journal_sequences_complete"] is True

    with journal_path.open("ab") as stream:
        stream.write(b"tampered-tail")
    tampered = verify_file(
        evidence_path,
        expected_release_label="0.3.0-pre.8",
        release_mode_evidence_path=envelope_path,
        require_release_mode=True,
    )
    assert tampered["gate_passed"] is False
    assert tampered["checks"]["mission_journal_hash_matches"] is False
    assert tampered["checks"]["mission_journal_integrity"] is False


def test_combined_sim_verifier_rejects_port_contract_drift(tmp_path: Path) -> None:
    evidence_path, envelope_path = _write_evidence_with_envelope(tmp_path)
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    document["ports"]["allocated_host_ports"][3] = 14_550  # type: ignore[index]
    document["ports"]["all_test_ports_released"] = False  # type: ignore[index]
    evidence_path.write_text(json.dumps(document), encoding="utf-8")

    result = verify_file(
        evidence_path,
        expected_release_label="0.3.0-pre.8",
        release_mode_evidence_path=envelope_path,
        require_release_mode=True,
    )

    assert result["gate_passed"] is False
    assert result["checks"]["named_ports_match"] is False
    assert result["checks"]["protected_port_unchanged"] is False
    assert result["checks"]["test_ports_released"] is False
    assert any("protected UDP 14550" in reason for reason in result["reasons"])


def test_combined_sim_verifier_rejects_tampered_e7_source_hash(tmp_path: Path) -> None:
    evidence_path, envelope_path = _write_evidence_with_envelope(tmp_path)
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["source"]["sha256"] = "0" * 64
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")

    result = verify_file(
        evidence_path,
        expected_release_label="0.3.0-pre.8",
        release_mode_evidence_path=envelope_path,
        require_release_mode=True,
    )

    assert result["gate_passed"] is False
    assert result["checks"]["release_mode_source_hash_matches"] is False
    assert any("source SHA-256" in reason for reason in result["reasons"])


@pytest.mark.parametrize(
    ("field", "tampered_value", "failed_check"),
    (
        (
            "camera_info_intrinsics_semantic_validation",
            "pending_k_p_distortion_validation",
            "camera_info_intrinsics_semantics_validated",
        ),
        ("received_frame_count", 2, "sensor_frames_complete"),
        ("odometry_timestamp_frame_count", 2, "odometry_frames_complete"),
    ),
)
def test_combined_sim_verifier_rejects_incomplete_sensor_semantics(
    tmp_path: Path,
    field: str,
    tampered_value: object,
    failed_check: str,
) -> None:
    document = _combined_document()
    document["integrated_sim_qgc"]["sensor_transport"][field] = tampered_value  # type: ignore[index]
    evidence_path = tmp_path / f"combined-{field}.json"
    evidence_path.write_text(json.dumps(document), encoding="utf-8")

    result = verify_file(evidence_path, expected_release_label="0.3.0-pre.8")

    assert result["gate_passed"] is False
    assert result["checks"][failed_check] is False


def test_combined_sim_verifier_rejects_tampered_target_evidence_card(
    tmp_path: Path,
) -> None:
    document = _combined_document()
    card = document["integrated_sim_qgc"]["target_geolocation_acceptance"]["target_evidence_cards"][
        0
    ]
    card["claims"]["contains_flight_control"] = True  # type: ignore[index]
    evidence_path = tmp_path / "combined-tampered-target-card.json"
    evidence_path.write_text(json.dumps(document), encoding="utf-8")

    result = verify_file(evidence_path, expected_release_label="0.3.0-pre.8")

    assert result["gate_passed"] is False
    assert result["checks"]["target_evidence_cards_contract"] is False
    assert any("target evidence card 0 is invalid" in reason for reason in result["reasons"])


def test_combined_sim_verifier_rejects_target_card_run_identity_drift(
    tmp_path: Path,
) -> None:
    document = _combined_document()
    document["integrated_sim_qgc"]["target_geolocation_acceptance"]["target_evidence_cards"][0][
        "run_identity_sha256"
    ] = "b" * 64  # type: ignore[index]
    evidence_path = tmp_path / "combined-target-card-identity-drift.json"
    evidence_path.write_text(json.dumps(document), encoding="utf-8")

    result = verify_file(evidence_path, expected_release_label="0.3.0-pre.8")

    assert result["gate_passed"] is False
    assert result["checks"]["target_evidence_cards_contract"] is False
    assert any("run identity" in reason for reason in result["reasons"])


def test_combined_sim_verifier_rejects_hardware_or_visual_claims(tmp_path: Path) -> None:
    evidence_path, envelope_path = _write_evidence_with_envelope(tmp_path)
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    document["integrated_sim_qgc"]["hardware_outputs_present"] = True  # type: ignore[index]
    document["qgc"]["operator_visual_acceptance"] = "passed"  # type: ignore[index]
    evidence_path.write_text(json.dumps(document), encoding="utf-8")

    result = verify_file(
        evidence_path,
        expected_release_label="0.3.0-pre.8",
        release_mode_evidence_path=envelope_path,
        require_release_mode=True,
    )

    assert result["gate_passed"] is False
    assert result["checks"]["no_hardware_claims"] is False
    assert result["checks"]["no_operator_visual_claim"] is False


@pytest.mark.parametrize(
    ("claim_key", "claim_value"),
    (
        ("qml_rendering_proved", True),
        ("complete_qml_rendering_semantics_proved", True),
        ("exact_source_frame_identity_proved", True),
        ("machine_visual_smoke", "passed"),
    ),
)
def test_combined_sim_verifier_recursively_rejects_unproved_visual_identity_claims(
    tmp_path: Path,
    claim_key: str,
    claim_value: object,
) -> None:
    document = _combined_document()
    document["arbitrarily_nested_evidence"] = {
        "levels": [
            {"benign": False},
            {"deeper": {"claim": {claim_key: claim_value}}},
        ]
    }
    evidence_path = tmp_path / f"combined-{claim_key}.json"
    evidence_path.write_text(json.dumps(document), encoding="utf-8")

    result = verify_file(
        evidence_path,
        expected_release_label="0.3.0-pre.8",
    )

    expected_path = f"$.arbitrarily_nested_evidence.levels[1].deeper.claim.{claim_key}"
    assert result["gate_passed"] is False
    assert result["checks"]["no_unproved_visual_identity_claims"] is False
    assert result["checks"]["no_operator_visual_claim"] is False
    assert any(expected_path in reason for reason in result["reasons"])


def test_combined_sim_verifier_accepts_historical_software_only_visual_boundaries(
    tmp_path: Path,
) -> None:
    document = _combined_document()
    document["historical_software_only_visual_evidence"] = {
        "nested": [
            {
                "operator_visual_acceptance": "not_claimed",
                "qgc_fly_view_visual_acceptance": "not_claimed",
                "qml_rendering_proved": False,
                "complete_qml_rendering_semantics_proved": False,
                "exact_source_frame_identity_proved": False,
                "machine_visual_smoke": (
                    "passed_native_qquickitem_videoContent_pixels_and_transport_binding"
                ),
            }
        ]
    }
    evidence_path = tmp_path / "combined-software-only-boundaries.json"
    evidence_path.write_text(json.dumps(document), encoding="utf-8")

    result = verify_file(
        evidence_path,
        expected_release_label="0.3.0-pre.8",
    )

    assert result["gate_passed"] is True
    assert result["checks"]["no_unproved_visual_identity_claims"] is True
    assert result["checks"]["no_operator_visual_claim"] is True


def test_combined_sim_verifier_fails_closed_for_missing_envelope(tmp_path: Path) -> None:
    evidence_path = tmp_path / "combined-evidence.json"
    evidence_path.write_text(json.dumps(_combined_document()), encoding="utf-8")

    result = verify_file(
        evidence_path,
        expected_release_label="0.3.0-pre.8",
        require_release_mode=True,
    )

    assert result["gate_passed"] is False
    assert result["checks"]["release_mode_envelope_present"] is False
    assert any(
        "release-mode evidence envelope is required" in reason for reason in result["reasons"]
    )


def test_combined_sim_verifier_cli_can_write_report(tmp_path: Path, capsys) -> None:
    evidence_path, envelope_path = _write_evidence_with_envelope(tmp_path)
    report_path = tmp_path / "reports" / "combined-verification.json"

    exit_code = main(
        [
            str(evidence_path),
            "--expected-release-label",
            "0.3.0-pre.8",
            "--release-mode-evidence",
            str(envelope_path),
            "--require-release-mode",
            "--out",
            str(report_path),
        ]
    )

    assert exit_code == 0
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["gate_passed"] is True
    assert json.loads(capsys.readouterr().out)["gate_passed"] is True

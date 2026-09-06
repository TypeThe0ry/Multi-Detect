from __future__ import annotations

import copy
import json
from pathlib import Path

from multidetect.simulator.safety_evidence import run_acceptance, verify_evidence


def test_simulator_safety_acceptance_passes_all_s4_checks() -> None:
    evidence = run_acceptance()

    assert evidence["schema_version"] == 1
    assert evidence["event"] == "simulator_safety_acceptance_finished"
    assert evidence["runtime_mode"] == "sim"
    assert evidence["release_label"].startswith("0.3.0-pre.")
    assert evidence["truth_free"] is True
    assert evidence["gate_passed"] is True
    assert all(evidence["checks"].values())

    verified = verify_evidence(evidence)
    assert verified["gate_passed"] is True
    assert verified["reasons"] == []


def test_simulator_safety_acceptance_is_deterministic() -> None:
    first = run_acceptance()
    second = run_acceptance()

    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )
    assert "generated_at_utc" not in first


def test_verifier_recomputes_cancel_gate_after_tamper() -> None:
    tampered = copy.deepcopy(run_acceptance())
    tampered["cancel"]["cancel"]["tracked_target_ids_after_cancel"] = ["target-000001"]

    verified = verify_evidence(tampered)

    assert verified["gate_passed"] is False
    assert any("tracked_target_ids_after_cancel" in reason for reason in verified["reasons"])


def test_verifier_recomputes_control_gate_after_type_tamper() -> None:
    tampered = copy.deepcopy(run_acceptance())
    tampered["control"]["interactive_modes"][0]["qgc_non_loopback_datagrams_blocked"] = "1"

    verified = verify_evidence(tampered)

    assert verified["gate_passed"] is False
    assert any("must be an integer" in reason for reason in verified["reasons"])


def test_verifier_rejects_hardware_or_visual_claims() -> None:
    tampered = copy.deepcopy(run_acceptance())
    tampered["claims"]["hardware_acceptance"] = "pass"
    tampered["claims"]["operator_visual_acceptance"] = "pass"

    verified = verify_evidence(tampered)

    assert verified["gate_passed"] is False
    assert any("claims.hardware_acceptance" in reason for reason in verified["reasons"])
    assert any("claims.operator_visual_acceptance" in reason for reason in verified["reasons"])


def test_verifier_requires_each_interactive_safety_flag() -> None:
    for key, value, fragment in (
        ("interactive_gate_passed", False, "interactive_gate_passed"),
        ("gate_passed", False, "interactive_modes[0].gate_passed"),
        ("loopback_only", False, "interactive_modes[0].loopback_only"),
        ("qgc_mission_writes_allowed", True, "qgc_mission_writes_allowed"),
        ("qgc_flight_commands_allowed", True, "qgc_flight_commands_allowed"),
        ("watchdog_failsafe", False, "watchdog_failsafe"),
        ("neutral_watchdog_frame", False, "neutral_watchdog_frame"),
    ):
        tampered = copy.deepcopy(run_acceptance())
        if key == "interactive_gate_passed":
            tampered["control"][key] = value
        else:
            tampered["control"]["interactive_modes"][0][key] = value

        verified = verify_evidence(tampered)

        assert verified["gate_passed"] is False
        assert any(fragment in reason for reason in verified["reasons"])


def test_verifier_requires_direct_watchdog_contract() -> None:
    for key, value, fragment in (
        ("no_control_after_failsafe", False, "no_control_after_failsafe"),
        ("watchdog_neutral_emitted", False, "watchdog_neutral_emitted"),
        ("loopback_endpoint", "udpout:192.168.1.1:14800", "loopback_endpoint"),
    ):
        tampered = copy.deepcopy(run_acceptance())
        tampered["control"]["direct_watchdog"][key] = value

        verified = verify_evidence(tampered)

        assert verified["gate_passed"] is False
        assert any(fragment in reason for reason in verified["reasons"])


def test_verifier_checks_cancel_identity_and_geolocation_reasons() -> None:
    cancel_tampered = copy.deepcopy(run_acceptance())
    cancel_tampered["cancel"]["lck_target_id"] = "target-unknown"
    cancel_verified = verify_evidence(cancel_tampered)
    assert cancel_verified["gate_passed"] is False
    assert any("lck_target_id" in reason for reason in cancel_verified["reasons"])

    geo_tampered = copy.deepcopy(run_acceptance())
    geo_tampered["geolocation"]["safety_denial_cases"][0]["denial_reasons"] = []
    geo_verified = verify_evidence(geo_tampered)
    assert geo_verified["gate_passed"] is False
    assert any("denial_reasons" in reason for reason in geo_verified["reasons"])


def test_safety_evidence_schema_is_valid_json() -> None:
    schema_path = (
        Path(__file__).parents[1] / "configs" / "schemas" / "simulator-safety-evidence.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["gate_passed"]["type"] == "boolean"

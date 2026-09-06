from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import multidetect.field_link_preflight as field_link_preflight
from multidetect.field_link_address_state import FIELD_LINK_ADDRESS_STATE_SCHEMA
from multidetect.field_link_preflight import main, verify_field_link_report


def _applied_address_document(observed_at: datetime) -> dict[str, object]:
    return {
        "schema": FIELD_LINK_ADDRESS_STATE_SCHEMA,
        "schema_version": 1,
        "generated_at_utc": observed_at.astimezone(timezone.utc).isoformat(),
        "computer_name": "FIELD-LAPTOP",
        "interface_alias": "Ethernet",
        "interface_index": 7,
        "interface_status": "Up",
        "link_speed": "1 Gbps",
        "addresses": [
            {
                "ip_address": "192.168.1.13",
                "prefix_length": 24,
                "address_state": "Preferred",
                "skip_as_source": False,
            },
            {
                "ip_address": "192.168.144.250",
                "prefix_length": 24,
                "address_state": "Preferred",
                "skip_as_source": True,
            },
        ],
        "aircraft_route": [],
        "execution": {
            "mode": "apply",
            "operation": "add-secondary-address",
            "changed": True,
            "requested_address": "192.168.144.250",
            "requested_prefix_length": 24,
            "postcondition_checked": True,
            "postcondition_passed": True,
            "output": None,
        },
    }


def _passing_report(observed_epoch_s: float) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at_epoch_s": observed_epoch_s,
        "target": {"expected_source_cidr": "192.168.144.0/24"},
        "probes": [{"name": "jetson", "ok": True}],
        "summary": {
            "all_passed": True,
            "hardware_link_ready": True,
            "route_checks_requested": False,
            "route_checks_passed": None,
            "route_source_cidr": "192.168.144.0/24",
        },
        "notes": [],
    }


def _run_bound_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    address_document: dict[str, object],
    report_epoch_s: float,
    maximum_age_s: float = 300.0,
) -> tuple[Path, Path]:
    address_path = tmp_path / "address-state.json"
    output_path = tmp_path / "field-link.json"
    address_path.write_text(json.dumps(address_document), encoding="utf-8")
    monkeypatch.setattr(
        field_link_preflight,
        "build_field_link_report",
        lambda **_: _passing_report(report_epoch_s),
    )
    assert (
        main(
            [
                "--address-state",
                str(address_path),
                "--address-state-max-age-s",
                str(maximum_age_s),
                "--interface-alias",
                "Ethernet",
                "--expected-field-address",
                "192.168.144.250",
                "--expected-field-prefix-length",
                "24",
                "--expected-source-cidr",
                "192.168.144.0/24",
                "--out",
                str(output_path),
            ]
        )
        == 0
    )
    return address_path, output_path


def test_cli_hash_binds_fresh_applied_address_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_at = datetime.now(timezone.utc)
    address_path, output_path = _run_bound_cli(
        monkeypatch,
        tmp_path,
        address_document=_applied_address_document(observed_at),
        report_epoch_s=observed_at.timestamp() + 1.0,
    )
    original_report = json.loads(output_path.read_text(encoding="utf-8"))
    report = copy.deepcopy(original_report)
    binding = report["address_state"]
    assert binding["required"] is True
    assert binding["mode"] == "apply"
    assert binding["application_succeeded"] is True
    assert binding["report_path"] == str(address_path.resolve())
    assert verify_field_link_report(output_path)["gate_passed"] is True


def test_parent_verifier_reopens_address_state_and_rejects_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_at = datetime.now(timezone.utc)
    address_path, output_path = _run_bound_cli(
        monkeypatch,
        tmp_path,
        address_document=_applied_address_document(observed_at),
        report_epoch_s=observed_at.timestamp() + 1.0,
    )
    changed = _applied_address_document(observed_at)
    changed["computer_name"] = "REPLACED-LAPTOP"
    address_path.write_text(json.dumps(changed), encoding="utf-8")
    result = verify_field_link_report(output_path)
    assert result["gate_passed"] is False
    assert "field-link address_state.report_sha256 does not match source" in result["reasons"]


def test_parent_verifier_rejects_binding_field_tamper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_at = datetime.now(timezone.utc)
    _, output_path = _run_bound_cli(
        monkeypatch,
        tmp_path,
        address_document=_applied_address_document(observed_at),
        report_epoch_s=observed_at.timestamp() + 1.0,
    )
    original_report = json.loads(output_path.read_text(encoding="utf-8"))
    report = copy.deepcopy(original_report)
    report["address_state"]["application_succeeded"] = False
    output_path.write_text(json.dumps(report), encoding="utf-8")
    result = verify_field_link_report(output_path)
    assert result["gate_passed"] is False
    assert "field-link address_state.application_succeeded must be true" in result["reasons"]

    report = copy.deepcopy(original_report)
    report["address_state"]["max_age_s_at_preflight"] = 301.0
    output_path.write_text(json.dumps(report), encoding="utf-8")
    result = verify_field_link_report(output_path)
    assert result["gate_passed"] is False
    assert "address_state binding values are invalid" in " ".join(result["reasons"])

    report = copy.deepcopy(original_report)
    del report["target"]
    output_path.write_text(json.dumps(report), encoding="utf-8")
    result = verify_field_link_report(output_path)
    assert result["gate_passed"] is False
    assert "requires a parent expected source CIDR" in " ".join(result["reasons"])


def test_cli_rejects_what_if_as_applied_address_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_at = datetime.now(timezone.utc)
    document = _applied_address_document(observed_at)
    document["addresses"].pop()
    document["execution"] = {
        "mode": "what-if",
        "operation": "add-secondary-address",
        "changed": False,
        "requested_address": "192.168.144.250",
        "requested_prefix_length": 24,
        "postcondition_checked": False,
        "postcondition_passed": None,
        "output": None,
    }
    address_path = tmp_path / "what-if.json"
    address_path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(
        field_link_preflight,
        "build_field_link_report",
        lambda **_: _passing_report(observed_at.timestamp() + 1.0),
    )
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "--address-state",
                str(address_path),
                "--interface-alias",
                "Ethernet",
                "--expected-source-cidr",
                "192.168.144.0/24",
            ]
        )
    assert raised.value.code == 2


def test_archived_parent_uses_preflight_time_for_address_freshness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    _, output_path = _run_bound_cli(
        monkeypatch,
        tmp_path,
        address_document=_applied_address_document(observed_at),
        report_epoch_s=observed_at.timestamp() + 10.0,
        maximum_age_s=30.0,
    )
    # Six-year-old evidence remains reproducible because the address receipt
    # is evaluated against its parent probe time, not today's wall clock.
    assert verify_field_link_report(output_path)["gate_passed"] is True


def test_cli_rejects_address_outside_expected_source_cidr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed_at = datetime.now(timezone.utc)
    document = _applied_address_document(observed_at)
    changed = copy.deepcopy(document)
    changed["addresses"][-1]["ip_address"] = "10.0.0.5"
    changed["execution"]["requested_address"] = "10.0.0.5"
    address_path = tmp_path / "wrong-subnet.json"
    address_path.write_text(json.dumps(changed), encoding="utf-8")
    monkeypatch.setattr(
        field_link_preflight,
        "build_field_link_report",
        lambda **_: _passing_report(observed_at.timestamp() + 1.0),
    )
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "--address-state",
                str(address_path),
                "--interface-alias",
                "Ethernet",
                "--expected-field-address",
                "10.0.0.5",
                "--expected-source-cidr",
                "192.168.144.0/24",
            ]
        )
    assert raised.value.code == 2

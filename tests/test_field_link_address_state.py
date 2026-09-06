from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from multidetect.field_link_address_state import (
    FIELD_LINK_ADDRESS_STATE_SCHEMA,
    main,
    verify_field_link_address_state,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "configs" / "schemas" / "field-link-address-state.schema.json"


def _base_document() -> dict[str, object]:
    return {
        "schema": FIELD_LINK_ADDRESS_STATE_SCHEMA,
        "schema_version": 1,
        "generated_at_utc": "2026-09-06T00:00:00+00:00",
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
            }
        ],
        "aircraft_route": [],
        "execution": {},
    }


def _plan_document() -> dict[str, object]:
    document = _base_document()
    document["execution"] = {
        "mode": "plan",
        "operation": "add-secondary-address",
        "would_change": True,
        "requires_elevation": True,
        "requested_address": "192.168.144.250",
        "requested_prefix_length": 24,
        "secondary_address_only": True,
        "skip_as_source": True,
        "default_gateway_change": False,
        "dns_change": False,
        "explicit_route_change": False,
        "rollback_operation": "remove-secondary-address",
        "rollback_command": (
            'powershell -File "D:\\repo\\scripts\\prepare_field_link.ps1" '
            '-InterfaceAlias "Ethernet" -IpAddress "192.168.144.250" '
            "-PrefixLength 24 -Remove -Elevate"
        ),
        "output": None,
    }
    return document


def _apply_add_document(*, passed: bool = True) -> dict[str, object]:
    document = _base_document()
    if passed:
        document["addresses"].append(
            {
                "ip_address": "192.168.144.250",
                "prefix_length": 24,
                "address_state": "Preferred",
                "skip_as_source": True,
            }
        )
    document["execution"] = {
        "mode": "apply",
        "operation": "add-secondary-address",
        "changed": passed,
        "requested_address": "192.168.144.250",
        "requested_prefix_length": 24,
        "postcondition_checked": True,
        "postcondition_passed": passed,
        "output": None,
    }
    return document


def test_schema_is_valid_and_accepts_all_execution_shapes() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    documents = [_plan_document(), _apply_add_document()]
    verify = _base_document()
    verify["execution"] = {"mode": "verify", "changed": False, "output": None}
    documents.append(verify)
    what_if = _apply_add_document(passed=False)
    what_if["execution"].update(
        {"mode": "what-if", "postcondition_checked": False, "postcondition_passed": None}
    )
    documents.append(what_if)
    for document in documents:
        validator.validate(document)


def test_plan_verifier_recomputes_change_and_safety_contract() -> None:
    result = verify_field_link_address_state(
        _plan_document(),
        expected_interface_alias="Ethernet",
        expected_address="192.168.144.250",
        expected_prefix_length=24,
    )
    assert result["gate_passed"] is True
    assert result["document_valid"] is True
    assert result["application_succeeded"] is False
    assert result["mode"] == "plan"

    for field, replacement in (
        ("would_change", False),
        ("requires_elevation", False),
        ("secondary_address_only", False),
        ("skip_as_source", False),
        ("default_gateway_change", True),
        ("dns_change", True),
        ("explicit_route_change", True),
        ("rollback_operation", "add-secondary-address"),
        ("rollback_command", "powershell -Elevate"),
    ):
        changed = copy.deepcopy(_plan_document())
        changed["execution"][field] = replacement
        assert verify_field_link_address_state(changed)["gate_passed"] is False


def test_plan_recomputes_noop_and_remove_semantics() -> None:
    add_noop = _plan_document()
    add_noop["addresses"].append(
        {
            "ip_address": "192.168.144.250",
            "prefix_length": 24,
            "address_state": "Preferred",
            "skip_as_source": True,
        }
    )
    add_noop["execution"]["would_change"] = False
    add_noop["execution"]["requires_elevation"] = False
    assert verify_field_link_address_state(add_noop)["gate_passed"] is True

    remove = copy.deepcopy(add_noop)
    remove["execution"].update(
        {
            "operation": "remove-secondary-address",
            "would_change": True,
            "requires_elevation": True,
            "rollback_operation": "add-secondary-address",
            "rollback_command": (
                'powershell -File "D:\\repo\\scripts\\prepare_field_link.ps1" '
                '-InterfaceAlias "Ethernet" -IpAddress "192.168.144.250" '
                "-PrefixLength 24 -Elevate"
            ),
        }
    )
    assert verify_field_link_address_state(remove)["gate_passed"] is True


def test_what_if_is_valid_but_never_satisfies_require_applied() -> None:
    document = _apply_add_document(passed=False)
    document["execution"].update(
        {"mode": "what-if", "postcondition_checked": False, "postcondition_passed": None}
    )
    assert verify_field_link_address_state(document)["gate_passed"] is True
    result = verify_field_link_address_state(document, require_applied=True)
    assert result["gate_passed"] is False
    assert result["application_succeeded"] is False


def test_apply_requires_persisted_postcondition_and_skip_as_source() -> None:
    applied = _apply_add_document()
    result = verify_field_link_address_state(applied, require_applied=True)
    assert result["gate_passed"] is True
    assert result["application_succeeded"] is True

    wrong_skip = copy.deepcopy(applied)
    wrong_skip["addresses"][-1]["skip_as_source"] = False
    assert verify_field_link_address_state(wrong_skip, require_applied=True)["gate_passed"] is False

    false_success = _apply_add_document(passed=False)
    assert false_success["execution"]["postcondition_passed"] is False
    result = verify_field_link_address_state(false_success)
    assert result["document_valid"] is True
    assert result["gate_passed"] is False


def test_remove_apply_requires_target_to_be_absent() -> None:
    document = _base_document()
    document["execution"] = {
        "mode": "apply",
        "operation": "remove-secondary-address",
        "changed": True,
        "requested_address": "192.168.144.250",
        "requested_prefix_length": 24,
        "postcondition_checked": True,
        "postcondition_passed": True,
        "output": None,
    }
    assert verify_field_link_address_state(document, require_applied=True)["gate_passed"] is True
    document["addresses"].append(
        {
            "ip_address": "192.168.144.250",
            "prefix_length": 24,
            "address_state": "Preferred",
            "skip_as_source": True,
        }
    )
    assert verify_field_link_address_state(document, require_applied=True)["gate_passed"] is False


def test_verifier_rejects_unknown_duplicate_and_nonfinite_json(tmp_path: Path) -> None:
    unknown = _plan_document()
    unknown["unexpected"] = True
    assert verify_field_link_address_state(unknown)["gate_passed"] is False

    duplicate_address = _plan_document()
    duplicate_address["addresses"].append(copy.deepcopy(duplicate_address["addresses"][0]))
    assert verify_field_link_address_state(duplicate_address)["gate_passed"] is False

    duplicate_json = tmp_path / "duplicate.json"
    duplicate_json.write_text('{"schema": "a", "schema": "b"}', encoding="utf-8")
    assert verify_field_link_address_state(duplicate_json)["gate_passed"] is False
    nonfinite_json = tmp_path / "nonfinite.json"
    nonfinite_json.write_text('{"value": NaN}', encoding="utf-8")
    assert verify_field_link_address_state(nonfinite_json)["gate_passed"] is False


def test_verifier_rejects_symbolic_link_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tmp_path / "link.json"
    path_type = type(link)
    original_is_symlink = path_type.is_symlink
    monkeypatch.setattr(
        path_type,
        "is_symlink",
        lambda self: self == link or original_is_symlink(self),
    )
    result = verify_field_link_address_state(link)
    assert result["gate_passed"] is False
    assert "must not be a symbolic link" in " ".join(result["reasons"])


def test_verifier_hashes_file_and_enforces_freshness(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_plan_document()), encoding="utf-8")
    now = datetime(2026, 9, 6, 0, 0, 10, tzinfo=timezone.utc)
    result = verify_field_link_address_state(path, max_age_s=30.0, now_utc=now)
    assert result["gate_passed"] is True
    assert result["report_path"] == str(path.resolve())
    assert result["report_bytes"] == path.stat().st_size
    assert len(result["report_sha256"]) == 64

    stale = verify_field_link_address_state(
        path,
        max_age_s=5.0,
        now_utc=now + timedelta(seconds=30),
    )
    assert stale["gate_passed"] is False


def test_cli_outputs_verification_and_require_applied_exit_code(tmp_path: Path, capsys) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_plan_document()), encoding="utf-8")
    assert main([str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["gate_passed"] is True
    assert main([str(path), "--require-applied"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["gate_passed"] is False

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIELD_LINK_ADDRESS_STATE_SCHEMA = "multidetect.field-link-address-state/v1"
FIELD_LINK_ADDRESS_STATE_SCHEMA_VERSION = 1


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _object_pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_document(
    source: Path | str | Mapping[str, object],
) -> tuple[object, str | None, int, str | None]:
    if isinstance(source, Mapping):
        return dict(source), None, 0, None
    requested = Path(source).expanduser()
    if requested.is_symlink():
        raise ValueError("field-link address state must not be a symbolic link")
    path = requested.resolve(strict=False)
    raw = path.read_bytes()
    document = json.loads(
        raw.decode("utf-8-sig"),
        object_pairs_hook=_object_pairs_no_duplicates,
        parse_constant=_reject_constant,
    )
    return document, str(path), len(raw), hashlib.sha256(raw).hexdigest()


def _exact_keys(value: object, expected: set[str]) -> bool:
    return isinstance(value, Mapping) and set(value) == expected


def _is_int(value: object, *, minimum: int | None = None, maximum: int | None = None) -> bool:
    if type(value) is not int:
        return False
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _canonical_ipv4(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return None
    if parsed.version != 4 or str(parsed) != value:
        return None
    return str(parsed)


def verify_field_link_address_state(
    source: Path | str | Mapping[str, object],
    *,
    require_applied: bool = False,
    expected_interface_alias: str | None = None,
    expected_address: str | None = None,
    expected_prefix_length: int | None = None,
    max_age_s: float | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Independently verify a saved ``prepare_field_link.ps1`` state document.

    This verifier performs no network or host mutation.  Plan/verify/what-if
    documents can prove their own structure without claiming that an address
    was applied.  An apply document passes only when its persisted address
    state agrees with its postcondition.
    """

    reasons: list[str] = []
    checks: dict[str, bool] = {}
    report_path: str | None = None
    report_bytes = 0
    report_sha256: str | None = None
    try:
        document, report_path, report_bytes, report_sha256 = _load_document(source)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "schema_version": FIELD_LINK_ADDRESS_STATE_SCHEMA_VERSION,
            "gate_passed": False,
            "document_valid": False,
            "application_succeeded": False,
            "checks": {"read_json": False},
            "reasons": [f"field-link address state could not be read: {exc}"],
            "warnings": [],
            "report_path": report_path,
            "report_bytes": report_bytes,
            "report_sha256": report_sha256,
            "mode": None,
            "operation": None,
        }

    top_keys = {
        "schema",
        "schema_version",
        "generated_at_utc",
        "computer_name",
        "interface_alias",
        "interface_index",
        "interface_status",
        "link_speed",
        "addresses",
        "aircraft_route",
        "execution",
    }
    checks["closed_top_level"] = _exact_keys(document, top_keys)
    if not checks["closed_top_level"]:
        reasons.append("field-link address state must use the closed v1 top-level shape")
    if not isinstance(document, Mapping):
        return {
            "schema_version": FIELD_LINK_ADDRESS_STATE_SCHEMA_VERSION,
            "gate_passed": False,
            "document_valid": False,
            "application_succeeded": False,
            "checks": checks,
            "reasons": reasons,
            "warnings": [],
            "report_path": report_path,
            "report_bytes": report_bytes,
            "report_sha256": report_sha256,
            "mode": None,
            "operation": None,
        }

    checks["schema"] = (
        document.get("schema") == FIELD_LINK_ADDRESS_STATE_SCHEMA
        and document.get("schema_version") == FIELD_LINK_ADDRESS_STATE_SCHEMA_VERSION
        and type(document.get("schema_version")) is int
    )
    if not checks["schema"]:
        reasons.append("field-link address state schema identity is unsupported")

    observed_at = _parse_utc(document.get("generated_at_utc"))
    checks["generated_at_utc"] = observed_at is not None
    if observed_at is None:
        reasons.append("generated_at_utc must be an offset-aware ISO-8601 timestamp")

    text_fields_valid = all(
        isinstance(document.get(field), str) and bool(str(document[field]).strip())
        for field in ("computer_name", "interface_alias", "interface_status", "link_speed")
    )
    checks["host_and_interface_text"] = text_fields_valid
    if not text_fields_valid:
        reasons.append("computer/interface identity fields must be non-empty strings")
    checks["interface_index"] = _is_int(document.get("interface_index"), minimum=1)
    if not checks["interface_index"]:
        reasons.append("interface_index must be a positive integer")

    addresses = document.get("addresses")
    address_records_valid = isinstance(addresses, list)
    address_keys = {"ip_address", "prefix_length", "address_state", "skip_as_source"}
    normalized_addresses: list[tuple[str, int, bool]] = []
    if address_records_valid:
        for record in addresses:
            valid = _exact_keys(record, address_keys)
            if valid:
                address = _canonical_ipv4(record.get("ip_address"))
                prefix = record.get("prefix_length")
                state = record.get("address_state")
                skip = record.get("skip_as_source")
                valid = (
                    address is not None
                    and _is_int(prefix, minimum=0, maximum=32)
                    and isinstance(state, str)
                    and bool(state.strip())
                    and type(skip) is bool
                )
                if valid:
                    normalized_addresses.append((address, prefix, skip))
            if not valid:
                address_records_valid = False
                break
    address_identities = [(item[0], item[1]) for item in normalized_addresses]
    checks["addresses"] = address_records_valid and len(address_identities) == len(
        set(address_identities)
    )
    if not checks["addresses"]:
        reasons.append("addresses must be unique, closed, canonical IPv4 records")

    routes = document.get("aircraft_route")
    route_records_valid = isinstance(routes, list)
    route_keys = {"destination_prefix", "next_hop", "route_metric", "policy_store"}
    if route_records_valid:
        for record in routes:
            valid = _exact_keys(record, route_keys)
            if valid:
                try:
                    destination = ipaddress.ip_network(
                        str(record.get("destination_prefix")), strict=False
                    )
                    next_hop = ipaddress.ip_address(str(record.get("next_hop")))
                    valid = (
                        destination.version == 4
                        and str(destination) == record.get("destination_prefix")
                        and next_hop.version == 4
                        and str(next_hop) == record.get("next_hop")
                        and _is_int(record.get("route_metric"), minimum=0)
                        and isinstance(record.get("policy_store"), str)
                        and bool(str(record["policy_store"]).strip())
                    )
                except ValueError:
                    valid = False
            if not valid:
                route_records_valid = False
                break
    checks["aircraft_route"] = route_records_valid
    if not route_records_valid:
        reasons.append("aircraft_route must contain closed canonical IPv4 route records")

    execution = document.get("execution")
    mode = execution.get("mode") if isinstance(execution, Mapping) else None
    operation = execution.get("operation") if isinstance(execution, Mapping) else None
    common_output_valid = isinstance(execution, Mapping) and (
        execution.get("output") is None
        or (isinstance(execution.get("output"), str) and bool(execution["output"].strip()))
    )
    execution_valid = False
    application_succeeded = False
    requested_address: str | None = None
    requested_prefix: int | None = None
    if mode == "verify":
        execution_valid = (
            _exact_keys(execution, {"mode", "changed", "output"})
            and execution.get("changed") is False
            and common_output_valid
        )
    elif mode == "plan":
        plan_keys = {
            "mode",
            "operation",
            "would_change",
            "requires_elevation",
            "requested_address",
            "requested_prefix_length",
            "secondary_address_only",
            "skip_as_source",
            "default_gateway_change",
            "dns_change",
            "explicit_route_change",
            "rollback_operation",
            "rollback_command",
            "output",
        }
        requested_address = _canonical_ipv4(execution.get("requested_address"))
        requested_prefix = execution.get("requested_prefix_length")
        matches = (
            sum(
                item[0] == requested_address and item[1] == requested_prefix
                for item in normalized_addresses
            )
            if requested_address is not None and _is_int(requested_prefix, minimum=1, maximum=32)
            else -1
        )
        expected_change = matches > 0 if operation == "remove-secondary-address" else matches == 0
        expected_rollback = (
            "add-secondary-address"
            if operation == "remove-secondary-address"
            else "remove-secondary-address"
        )
        rollback_command = execution.get("rollback_command")
        rollback_remove = (
            isinstance(rollback_command, str) and " -Remove " in f" {rollback_command} "
        )
        rollback_valid = (
            isinstance(rollback_command, str)
            and bool(rollback_command.strip())
            and f'-InterfaceAlias "{document.get("interface_alias")}"' in rollback_command
            and f'-IpAddress "{requested_address}"' in rollback_command
            and f"-PrefixLength {requested_prefix}" in rollback_command
            and "-Elevate" in rollback_command
            and rollback_remove == (expected_rollback == "remove-secondary-address")
        )
        execution_valid = (
            _exact_keys(execution, plan_keys)
            and operation in {"add-secondary-address", "remove-secondary-address"}
            and requested_address is not None
            and _is_int(requested_prefix, minimum=1, maximum=32)
            and type(execution.get("would_change")) is bool
            and execution.get("would_change") is expected_change
            and execution.get("requires_elevation") is expected_change
            and execution.get("secondary_address_only") is True
            and execution.get("skip_as_source") is True
            and execution.get("default_gateway_change") is False
            and execution.get("dns_change") is False
            and execution.get("explicit_route_change") is False
            and execution.get("rollback_operation") == expected_rollback
            and rollback_valid
            and common_output_valid
        )
    elif mode in {"apply", "what-if"}:
        apply_keys = {
            "mode",
            "operation",
            "changed",
            "requested_address",
            "requested_prefix_length",
            "postcondition_checked",
            "postcondition_passed",
            "output",
        }
        requested_address = _canonical_ipv4(execution.get("requested_address"))
        requested_prefix = execution.get("requested_prefix_length")
        matches = [
            item
            for item in normalized_addresses
            if item[0] == requested_address and item[1] == requested_prefix
        ]
        expected_postcondition = (
            len(matches) == 0
            if operation == "remove-secondary-address"
            else len(matches) == 1 and matches[0][2] is True
        )
        if mode == "what-if":
            mode_fields_valid = (
                execution.get("changed") is False
                and execution.get("postcondition_checked") is False
                and execution.get("postcondition_passed") is None
            )
        else:
            mode_fields_valid = (
                type(execution.get("changed")) is bool
                and execution.get("postcondition_checked") is True
                and type(execution.get("postcondition_passed")) is bool
                and execution.get("postcondition_passed") is expected_postcondition
            )
            application_succeeded = mode_fields_valid and expected_postcondition
        execution_valid = (
            _exact_keys(execution, apply_keys)
            and operation in {"add-secondary-address", "remove-secondary-address"}
            and requested_address is not None
            and _is_int(requested_prefix, minimum=1, maximum=32)
            and mode_fields_valid
            and common_output_valid
        )
    checks["execution"] = execution_valid
    if not execution_valid:
        reasons.append("execution record is invalid or inconsistent with persisted address state")

    if expected_interface_alias is not None:
        checks["expected_interface_alias"] = (
            document.get("interface_alias") == expected_interface_alias
        )
        if not checks["expected_interface_alias"]:
            reasons.append("interface_alias does not match the expected interface")
    if expected_address is not None:
        expected_address_value = _canonical_ipv4(expected_address)
        checks["expected_address"] = (
            expected_address_value is not None and requested_address == expected_address_value
        )
        if not checks["expected_address"]:
            reasons.append("requested_address does not match the expected address")
    if expected_prefix_length is not None:
        checks["expected_prefix_length"] = requested_prefix == expected_prefix_length
        if not checks["expected_prefix_length"]:
            reasons.append("requested_prefix_length does not match the expected prefix")

    if max_age_s is not None:
        age_valid = (
            isinstance(max_age_s, (int, float))
            and not isinstance(max_age_s, bool)
            and math.isfinite(float(max_age_s))
            and float(max_age_s) >= 0.0
        )
        if not age_valid:
            raise ValueError("max_age_s must be finite and non-negative")
        reference = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        age_s = (reference - observed_at).total_seconds() if observed_at is not None else math.inf
        checks["freshness"] = -1.0 <= age_s <= float(max_age_s)
        if not checks["freshness"]:
            reasons.append("field-link address state is stale or from the future")

    document_valid = all(checks.values())
    if mode == "apply" and document_valid and not application_succeeded:
        reasons.append("apply postcondition did not pass")
    if require_applied and mode != "apply":
        checks["require_applied"] = False
        reasons.append("an applied field-link address state is required")
    elif require_applied:
        checks["require_applied"] = application_succeeded
        if not application_succeeded and "apply postcondition did not pass" not in reasons:
            reasons.append("applied field-link address state did not succeed")

    gate_passed = all(checks.values()) and (mode != "apply" or application_succeeded)
    return {
        "schema_version": FIELD_LINK_ADDRESS_STATE_SCHEMA_VERSION,
        "gate_passed": gate_passed,
        "document_valid": document_valid,
        "application_succeeded": application_succeeded,
        "checks": checks,
        "reasons": reasons,
        "warnings": [],
        "report_path": report_path,
        "report_bytes": report_bytes,
        "report_sha256": report_sha256,
        "mode": mode,
        "operation": operation,
        "document_schema": document.get("schema"),
        "generated_at_utc": document.get("generated_at_utc"),
        "interface_alias": document.get("interface_alias"),
        "requested_address": requested_address,
        "requested_prefix_length": requested_prefix,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a saved Multi-Detect field-link address state without changing the host."
        )
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("--require-applied", action="store_true")
    parser.add_argument("--expected-interface-alias")
    parser.add_argument("--expected-address")
    parser.add_argument("--expected-prefix-length", type=int)
    parser.add_argument("--max-age-s", type=float)
    args = parser.parse_args(argv)
    try:
        result = verify_field_link_address_state(
            args.report,
            require_applied=args.require_applied,
            expected_interface_alias=args.expected_interface_alias,
            expected_address=args.expected_address,
            expected_prefix_length=args.expected_prefix_length,
            max_age_s=args.max_age_s,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FIELD_LINK_ADDRESS_STATE_SCHEMA",
    "FIELD_LINK_ADDRESS_STATE_SCHEMA_VERSION",
    "main",
    "verify_field_link_address_state",
]

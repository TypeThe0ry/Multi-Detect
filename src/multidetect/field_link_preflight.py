from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
import math
import re
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .field_link_address_state import verify_field_link_address_state

FIELD_LINK_REPORT_SCHEMA_VERSION = 1
FIELD_LINK_DISCOVERY_SCHEMA_VERSION = 1
FIELD_LINK_INTERFACE_DIAGNOSTIC_SCHEMA_VERSION = 1
DEFAULT_DISCOVERY_MAX_HOSTS = 256
DEFAULT_DISCOVERY_WORKERS = 32
DEFAULT_FIELD_LINK_MAX_AGE_S = 900.0
MAX_FIELD_LINK_ADDRESS_BINDING_AGE_S = 300.0


@dataclass(frozen=True, slots=True)
class EndpointProbe:
    name: str
    transport: str
    host: str
    port: int
    ok: bool
    elapsed_ms: float
    detail: str


@dataclass(frozen=True, slots=True)
class RouteProbe:
    """Passive local-route evidence for one remote field endpoint.

    A UDP ``connect`` only asks the operating system which local interface it
    would use; it does not transmit a datagram.  Keeping this as a separate
    record prevents a route diagnostic from being confused with TCP service
    reachability or a MAVLink/RTSP protocol check.
    """

    name: str
    transport: str
    host: str
    port: int
    ok: bool
    source_address: str | None
    elapsed_ms: float
    detail: str


def _sha256_file(path: Path) -> tuple[str | None, int]:
    """Return a file digest and byte count without following directories."""

    if not path.exists() or not path.is_file():
        return None, 0
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return None, size
    return digest.hexdigest(), size


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _is_finite_number(
    value: object,
    *,
    minimum: float | None = None,
    strict_minimum: bool = False,
) -> bool:
    """Validate a JSON number without letting huge integers escape as errors."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    if not math.isfinite(number):
        return False
    if minimum is None:
        return True
    return number > minimum if strict_minimum else number >= minimum


def verify_field_link_discovery(discovery: object) -> dict[str, Any]:
    """Verify the internal invariants of an optional discovery snapshot.

    Discovery is deliberately diagnostic: it may identify responsive hosts,
    but it never chooses aircraft endpoints and never upgrades the parent
    report's ``hardware_link_ready`` flag.  The normal field-link verifier
    calls this helper when a report contains a ``discovery`` object so a
    hand-edited service count, candidate list, or out-of-scope host cannot be
    mistaken for fresh link evidence.

    This function is intentionally network-free.  It validates only the
    persisted JSON shape and deterministic relationships produced by
    :func:`discover_field_link_hosts`.
    """

    reasons: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}
    if not isinstance(discovery, Mapping):
        return {
            "schema_version": FIELD_LINK_DISCOVERY_SCHEMA_VERSION,
            "present": True,
            "gate_passed": False,
            "checks": {"object": False},
            "reasons": ["field-link discovery must be a JSON object"],
            "warnings": [],
            "summary": None,
        }

    schema_version = discovery.get("schema_version")
    checks["schema_version"] = (
        type(schema_version) is int
        and not isinstance(schema_version, bool)
        and schema_version == FIELD_LINK_DISCOVERY_SCHEMA_VERSION
    )
    if not checks["schema_version"]:
        reasons.append("field-link discovery schema_version is unsupported")

    checks["mode"] = discovery.get("mode") == "discovery"
    if not checks["mode"]:
        reasons.append("field-link discovery mode must be 'discovery'")

    cidr = discovery.get("cidr")
    network: ipaddress.IPv4Network | None = None
    if isinstance(cidr, str) and cidr.strip():
        try:
            parsed_network = ipaddress.ip_network(cidr.strip(), strict=False)
            if parsed_network.version != 4:
                raise ValueError("field-link discovery cidr must be an IPv4 CIDR")
            canonical_cidr = str(parsed_network)
            if canonical_cidr != cidr.strip():
                raise ValueError("field-link discovery cidr must be canonical")
            network = parsed_network
        except ValueError as exc:
            reasons.append(str(exc))
    else:
        reasons.append("field-link discovery cidr must be a non-empty IPv4 CIDR")
    checks["cidr"] = network is not None

    ports = discovery.get("ports")
    expected_services = ("ssh", "mavlink", "rtsp")
    ports_valid = isinstance(ports, Mapping)
    if ports_valid:
        for service in expected_services:
            port = ports.get(service)
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                ports_valid = False
                reasons.append(
                    f"field-link discovery ports.{service} must be an integer in [1, 65535]"
                )
    else:
        reasons.append("field-link discovery ports must be an object")
    checks["ports"] = ports_valid

    timeout_s = discovery.get("timeout_s")
    checks["timeout"] = _is_finite_number(timeout_s, minimum=0.0, strict_minimum=True)
    if not checks["timeout"]:
        reasons.append("field-link discovery timeout_s must be finite and greater than zero")

    protocol_checks = discovery.get("protocol_checks")
    checks["protocol_checks"] = type(protocol_checks) is bool
    if not checks["protocol_checks"]:
        reasons.append("field-link discovery protocol_checks must be a boolean")

    max_hosts = discovery.get("max_hosts")
    workers = discovery.get("workers")
    bounds_valid = (
        type(max_hosts) is int
        and not isinstance(max_hosts, bool)
        and max_hosts > 0
        and type(workers) is int
        and not isinstance(workers, bool)
        and workers > 0
        and workers <= max_hosts
    )
    if not bounds_valid:
        reasons.append(
            "field-link discovery max_hosts/workers must be positive integers with "
            "workers <= max_hosts"
        )
    checks["bounds"] = bounds_valid

    scanned_hosts = discovery.get("scanned_hosts")
    responsive_hosts = discovery.get("responsive_hosts")
    counts_valid = (
        type(scanned_hosts) is int
        and not isinstance(scanned_hosts, bool)
        and scanned_hosts >= 0
        and type(responsive_hosts) is int
        and not isinstance(responsive_hosts, bool)
        and 0 <= responsive_hosts <= scanned_hosts
    )
    if not counts_valid:
        reasons.append(
            "field-link discovery scanned_hosts/responsive_hosts must be bounded integers"
        )
    checks["counts"] = counts_valid

    service_hits = discovery.get("service_hits")
    candidates_present = "service_candidates" in discovery
    service_candidates = discovery.get("service_candidates")
    aggregates_shape_valid = (
        isinstance(service_hits, Mapping)
        and all(service in service_hits for service in expected_services)
        and (
            not candidates_present
            or (
                isinstance(service_candidates, Mapping)
                and all(service in service_candidates for service in expected_services)
            )
        )
    )
    if not aggregates_shape_valid:
        reasons.append(
            "field-link discovery service_hits/service_candidates must contain "
            "ssh, mavlink and rtsp"
        )
    elif not candidates_present:
        # Early schema-v1 discovery snapshots predate ``service_candidates``.
        # Keep those historical files readable, but expose that only the
        # service-hit counters (not the candidate lists) were verified.
        warnings.append(
            "legacy field-link discovery omitted service_candidates; candidate-list "
            "integrity was not available in the source snapshot"
        )
    checks["aggregate_shape"] = aggregates_shape_valid
    checks["candidate_lists_present"] = candidates_present

    expected_host_count: int | None = None
    if network is not None:
        # Avoid enumerating a forged /0 or /8 during offline verification.  A
        # discovery report is bounded by ``max_hosts`` at creation time, but
        # the verifier must remain cheap even when an attacker edits the CIDR
        # and count fields before a report is rechecked.
        expected_host_count = (
            network.num_addresses if network.prefixlen >= 31 else network.num_addresses - 2
        )
    if counts_valid and expected_host_count is not None and scanned_hosts != expected_host_count:
        reasons.append(
            "field-link discovery scanned_hosts does not match the explicit CIDR host count"
        )
        checks["scanned_scope"] = False
    else:
        checks["scanned_scope"] = expected_host_count is not None and counts_valid
    if (
        counts_valid
        and bounds_valid
        and max_hosts is not None
        and scanned_hosts is not None
        and scanned_hosts > max_hosts
    ):
        reasons.append("field-link discovery scanned_hosts exceeds max_hosts")
        checks["scanned_scope"] = False

    hosts = discovery.get("hosts")
    hosts_shape_valid = isinstance(hosts, list)
    if not hosts_shape_valid:
        reasons.append("field-link discovery hosts must be a list")
        hosts = []
    host_addresses: list[str] = []
    actual_service_hits = {service: 0 for service in expected_services}
    actual_service_candidates = {service: [] for service in expected_services}
    expected_transport = {
        service: service if protocol_checks is True else "tcp" for service in expected_services
    }
    for index, host_record in enumerate(hosts):
        record_valid = isinstance(host_record, Mapping)
        host_value = host_record.get("host") if record_valid else None
        host_ip: ipaddress.IPv4Address | None = None
        if isinstance(host_value, str):
            try:
                parsed_host = ipaddress.ip_address(host_value)
                if parsed_host.version != 4:
                    raise ValueError("host is not IPv4")
                host_ip = parsed_host
            except ValueError:
                host_ip = None
        if host_ip is None:
            reasons.append(f"field-link discovery hosts[{index}].host must be an IPv4 address")
            record_valid = False
        elif network is not None and host_ip not in network:
            reasons.append(f"field-link discovery host {host_value} is outside the explicit CIDR")
            record_valid = False
        if isinstance(host_value, str):
            host_addresses.append(host_value)

        services = host_record.get("services") if record_valid else None
        if not isinstance(services, Mapping):
            reasons.append(f"field-link discovery hosts[{index}].services must be an object")
            record_valid = False
            services = {}
        service_outcomes: dict[str, bool] = {}
        for service in expected_services:
            probe = services.get(service)
            probe_valid = isinstance(probe, Mapping)
            if not probe_valid:
                reasons.append(
                    f"field-link discovery hosts[{index}].services.{service} must be an object"
                )
                continue
            port = ports.get(service) if isinstance(ports, Mapping) else None
            elapsed_ms = probe.get("elapsed_ms")
            probe_valid = (
                probe.get("name") == service
                and probe.get("transport") == expected_transport[service]
                and probe.get("host") == host_value
                and probe.get("port") == port
                and type(probe.get("ok")) is bool
                and _is_finite_number(elapsed_ms, minimum=0.0)
                and isinstance(probe.get("detail"), str)
            )
            if not probe_valid:
                reasons.append(
                    f"field-link discovery hosts[{index}].services.{service} is malformed"
                )
                continue
            outcome = bool(probe["ok"])
            service_outcomes[service] = outcome
            if outcome:
                actual_service_hits[service] += 1
                actual_service_candidates[service].append(str(host_value))

        expected_responsive = any(service_outcomes.values())
        if not record_valid or type(host_record.get("responsive")) is not bool:
            reasons.append(f"field-link discovery hosts[{index}].responsive must be a boolean")
        elif host_record["responsive"] is not True:
            # ``hosts`` is intentionally filtered to responsive candidates by
            # the producer.  A record with all services down must therefore be
            # rejected rather than silently counted as a discovered host.
            reasons.append(f"field-link discovery host {host_value} responsive must be true")
        elif not expected_responsive:
            reasons.append(
                f"field-link discovery host {host_value} responsive flag does not match services"
            )

    hosts_shape_valid = hosts_shape_valid and len(hosts) == (
        responsive_hosts if counts_valid else -1
    )
    if not hosts_shape_valid:
        reasons.append(
            "field-link discovery responsive_hosts does not match the hosts record count"
        )
    checks["hosts"] = hosts_shape_valid
    unique_hosts = len(host_addresses) == len(set(host_addresses))
    ordered_hosts = host_addresses == sorted(
        host_addresses,
        key=lambda value: int(ipaddress.ip_address(value)) if _is_ipv4_address(value) else -1,
    )
    checks["host_order"] = unique_hosts and ordered_hosts
    if not unique_hosts:
        reasons.append("field-link discovery hosts must not contain duplicate addresses")
    if not ordered_hosts:
        reasons.append("field-link discovery hosts must be in deterministic IPv4 order")

    aggregates_match = False
    candidates_shape_valid = not candidates_present
    if aggregates_shape_valid:
        if candidates_present:
            candidates_shape_valid = all(
                isinstance(service_candidates.get(service), list)
                and all(isinstance(host, str) for host in service_candidates.get(service, []))
                and len(service_candidates.get(service, []))
                == len(set(service_candidates.get(service, [])))
                for service in expected_services
            )
            if not candidates_shape_valid:
                reasons.append(
                    "field-link discovery service_candidates contains malformed host lists"
                )
        aggregates_match = all(
            type(service_hits.get(service)) is int
            and not isinstance(service_hits.get(service), bool)
            and service_hits.get(service) == actual_service_hits[service]
            and (
                not candidates_present
                or service_candidates.get(service) == actual_service_candidates[service]
            )
            for service in expected_services
        )
        if not aggregates_match:
            reasons.append(
                "field-link discovery service_hits/service_candidates do not match host services"
            )
    checks["aggregates"] = aggregates_shape_valid and candidates_shape_valid and aggregates_match

    elapsed_ms = discovery.get("elapsed_ms")
    checks["elapsed"] = _is_finite_number(elapsed_ms, minimum=0.0)
    if not checks["elapsed"]:
        reasons.append("field-link discovery elapsed_ms must be finite and non-negative")

    notes = discovery.get("notes")
    checks["notes"] = isinstance(notes, list) and all(isinstance(note, str) for note in notes)
    if not checks["notes"]:
        reasons.append("field-link discovery notes must be a list of strings")

    checks["no_readiness_claim"] = "hardware_link_ready" not in discovery
    if not checks["no_readiness_claim"]:
        reasons.append("field-link discovery must not contain a hardware_link_ready claim")

    summary = {
        "cidr": cidr,
        "scanned_hosts": scanned_hosts,
        "responsive_hosts": responsive_hosts,
        "service_hits": dict(service_hits) if isinstance(service_hits, Mapping) else None,
        "service_candidates": (
            {service: list(service_candidates.get(service, [])) for service in expected_services}
            if isinstance(service_candidates, Mapping)
            else None
        ),
    }
    return {
        "schema_version": FIELD_LINK_DISCOVERY_SCHEMA_VERSION,
        "present": True,
        "gate_passed": not reasons,
        "checks": checks,
        "reasons": reasons,
        "warnings": warnings,
        "summary": summary,
    }


def _is_ipv4_address(value: object) -> bool:
    """Return whether *value* is a syntactically valid IPv4 address."""

    if not isinstance(value, str):
        return False
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def verify_field_link_interface_diagnostic(
    report: str | Path,
    *,
    expected_source_cidr: str | None = None,
    expected_interface_alias: str | None = None,
) -> dict[str, Any]:
    """Verify a read-only Windows interface diagnostic for evidence binding.

    ``inspect_field_link_interfaces.ps1`` is intentionally separate from the
    socket preflight: it describes the workstation's physical adapters and
    never changes network state.  This verifier makes that hand-off explicit
    by checking the diagnostic's hash, schema, CIDR normalization and
    deterministic selection result.  It performs no adapter or route query,
    so it is safe to call from release/evidence verifiers on any platform.

    A ``missing`` selection is a valid diagnostic result.  It means the
    workstation currently has no active physical adapter on the aircraft
    subnet; readiness remains false until the operator reconnects/configures
    the link.  The diagnostic itself never carries a readiness claim.
    """

    if expected_source_cidr is not None and not isinstance(expected_source_cidr, str):
        raise ValueError("expected_source_cidr must be a string or None")
    if expected_interface_alias is not None and not isinstance(expected_interface_alias, str):
        raise ValueError("expected_interface_alias must be a string or None")

    requested = Path(report).expanduser()
    reasons: list[str] = []
    checks: dict[str, bool] = {}
    try:
        if requested.is_symlink():
            reasons.append("field-link interface diagnostic must not be a symbolic link")
        source = requested.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        source = requested
        reasons.append(f"field-link interface diagnostic path cannot be resolved: {exc}")

    report_sha256, report_bytes = _sha256_file(source)
    checks["file"] = report_sha256 is not None and not requested.is_symlink()
    result: dict[str, Any] = {
        "schema_version": 1,
        "diagnostic_schema_version": None,
        "report_path": str(source),
        "report_sha256": report_sha256,
        "report_bytes": report_bytes,
        "read_only": False,
        "expected_source_cidr": None,
        "normalized_source_cidr": None,
        "selection": None,
        "interface_alias": expected_interface_alias,
        "checks": checks,
        "reasons": reasons,
        "gate_passed": False,
    }
    if report_sha256 is None:
        reasons.append("field-link interface diagnostic is unavailable")
        return result

    try:
        payload = source.read_bytes()
        raw = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        reasons.append(f"field-link interface diagnostic cannot be loaded: {exc}")
        return result
    if not isinstance(raw, Mapping):
        reasons.append("field-link interface diagnostic must contain a JSON object")
        return result

    diagnostic_schema_version = raw.get("schema_version")
    result["diagnostic_schema_version"] = diagnostic_schema_version
    checks["schema_version"] = diagnostic_schema_version == (
        FIELD_LINK_INTERFACE_DIAGNOSTIC_SCHEMA_VERSION
    )
    if not checks["schema_version"]:
        reasons.append("unsupported field-link interface diagnostic schema_version")

    read_only = raw.get("read_only")
    result["read_only"] = read_only is True
    checks["read_only"] = read_only is True
    if not checks["read_only"]:
        reasons.append("field-link interface diagnostic read_only must be true")

    if "hardware_link_ready" in raw:
        checks["no_readiness_claim"] = False
        reasons.append(
            "field-link interface diagnostic must not contain a hardware_link_ready claim"
        )
    else:
        checks["no_readiness_claim"] = True

    raw_expected_cidr = raw.get("expected_source_cidr")
    raw_normalized_cidr = raw.get("normalized_source_cidr")
    parsed_expected: ipaddress.IPv4Network | None = None
    parsed_normalized: ipaddress.IPv4Network | None = None
    if not isinstance(raw_expected_cidr, str) or not raw_expected_cidr.strip():
        reasons.append(
            "field-link interface diagnostic expected_source_cidr must be a non-empty IPv4 CIDR"
        )
    else:
        try:
            parsed_expected = ipaddress.ip_network(raw_expected_cidr.strip(), strict=False)
            if parsed_expected.version != 4:
                raise ValueError("expected_source_cidr must be an IPv4 CIDR")
        except ValueError as exc:
            reasons.append(str(exc))
    if not isinstance(raw_normalized_cidr, str) or not raw_normalized_cidr.strip():
        reasons.append(
            "field-link interface diagnostic normalized_source_cidr must be a non-empty IPv4 CIDR"
        )
    else:
        try:
            parsed_normalized = ipaddress.ip_network(raw_normalized_cidr.strip(), strict=False)
            if parsed_normalized.version != 4:
                raise ValueError("normalized_source_cidr must be an IPv4 CIDR")
        except ValueError as exc:
            reasons.append(str(exc))
    if parsed_expected is not None and parsed_normalized is not None:
        if str(parsed_expected) != str(parsed_normalized):
            reasons.append(
                "field-link interface diagnostic normalized_source_cidr does not match "
                "expected_source_cidr"
            )
        if raw_normalized_cidr.strip() != str(parsed_normalized):
            reasons.append(
                "field-link interface diagnostic normalized_source_cidr must be canonical"
            )
    if expected_source_cidr is not None:
        try:
            expected_network = ipaddress.ip_network(expected_source_cidr.strip(), strict=False)
            if expected_network.version != 4:
                raise ValueError("expected_source_cidr must be an IPv4 CIDR")
            if parsed_normalized is None or str(parsed_normalized) != str(expected_network):
                reasons.append(
                    "field-link interface diagnostic CIDR does not match field-link preflight"
                )
        except ValueError as exc:
            reasons.append(str(exc))
    result["expected_source_cidr"] = raw_expected_cidr
    result["normalized_source_cidr"] = raw_normalized_cidr
    checks["cidr"] = parsed_expected is not None and parsed_normalized is not None

    selection = raw.get("selection")
    selection_result: dict[str, Any] | None = None
    if not isinstance(selection, Mapping):
        reasons.append("field-link interface diagnostic selection must be an object")
    else:
        status = selection.get("status")
        candidate_count = selection.get("candidate_count")
        candidate_aliases = selection.get("candidate_aliases")
        aliases_valid = (
            isinstance(candidate_aliases, list)
            and all(isinstance(alias, str) and alias.strip() for alias in candidate_aliases)
            and len(candidate_aliases) == len(set(candidate_aliases))
        )
        count_valid = (
            type(candidate_count) is int
            and not isinstance(candidate_count, bool)
            and candidate_count >= 0
            and aliases_valid
            and candidate_count == len(candidate_aliases)
        )
        status_valid = status in {"missing", "ambiguous", "ready"}
        status_count_valid = (
            (status == "missing" and candidate_count == 0)
            or (status == "ready" and candidate_count == 1)
            or (status == "ambiguous" and isinstance(candidate_count, int) and candidate_count > 1)
        )
        if not status_valid:
            reasons.append(
                "field-link interface diagnostic selection.status must be missing, "
                "ambiguous or ready"
            )
        if not count_valid:
            reasons.append(
                "field-link interface diagnostic selection candidate_count/aliases are malformed"
            )
        elif not status_count_valid:
            reasons.append(
                "field-link interface diagnostic selection.status does not match candidate_count"
            )
        selection_result = {
            "status": status,
            "candidate_count": candidate_count,
            "candidate_aliases": list(candidate_aliases)
            if isinstance(candidate_aliases, list)
            else None,
        }
        result["selection"] = selection_result
        checks["selection"] = status_valid and count_valid and status_count_valid
        if expected_interface_alias is not None:
            alias = expected_interface_alias.strip()
            if not alias:
                reasons.append("expected_interface_alias must not be empty")
            elif (
                status in {"ready", "ambiguous"}
                and isinstance(candidate_aliases, list)
                and alias not in candidate_aliases
            ):
                reasons.append("expected_interface_alias is not one of the diagnostic candidates")
        checks["interface_alias"] = expected_interface_alias is None or bool(
            expected_interface_alias.strip()
        )
    adapters = raw.get("adapters")
    checks["adapters"] = isinstance(adapters, list)
    if not checks["adapters"]:
        reasons.append("field-link interface diagnostic adapters must be a list")
    routes = raw.get("target_routes")
    checks["target_routes"] = isinstance(routes, list)
    if not checks["target_routes"]:
        reasons.append("field-link interface diagnostic target_routes must be a list")

    result["gate_passed"] = not reasons
    return result


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object members in evidence files."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant {value}")


def verify_field_link_report(
    report: str | Path,
    *,
    require_ready: bool = True,
    max_age_s: float | None = None,
) -> dict[str, Any]:
    """Verify the shape and readiness of a saved field-link preflight.

    This is an evidence verifier, not a second network probe.  It binds a
    later field-session capture to the exact JSON report that was observed at
    link bring-up, checks the report's fail-closed summary invariants, and
    returns the report SHA-256 for provenance.  When ``max_age_s`` is supplied,
    the report's observation timestamp is also checked against that bounded
    window (``finished_at_epoch_s`` for a watch, ``generated_at_epoch_s`` for a
    one-shot report).  Re-running this function never logs in, opens RTSP
    playback, requests MAVLink streams, or sends control data.
    """

    if type(require_ready) is not bool:
        raise ValueError("require_ready must be a boolean")
    if max_age_s is not None:
        if isinstance(max_age_s, bool) or not isinstance(max_age_s, (int, float)):
            raise ValueError("max_age_s must be a finite non-negative number or None")
        try:
            max_age_s = float(max_age_s)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("max_age_s must be a finite non-negative number or None") from exc
        if not math.isfinite(max_age_s) or max_age_s < 0.0:
            raise ValueError("max_age_s must be a finite non-negative number or None")
    requested_report_path = Path(report).expanduser()
    # Evidence files are provenance inputs.  Resolve their path for stable
    # reporting, but do not silently follow a reparse point/symbolic link: a
    # later verifier must hash the exact regular file the preflight produced,
    # not an attacker-controlled target swapped underneath that path.
    try:
        report_is_symlink = requested_report_path.is_symlink()
    except OSError:
        report_is_symlink = False
    reasons: list[str] = []
    try:
        report_path = requested_report_path.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report_path = requested_report_path
        reasons.append(f"field-link preflight report path cannot be resolved: {exc}")
    checks: dict[str, Any] = {}
    report_sha256, report_bytes = _sha256_file(report_path)
    checks["file"] = {
        "path": str(report_path),
        "available": report_sha256 is not None,
        "symlink": report_is_symlink,
        "sha256": report_sha256,
        "bytes": report_bytes,
    }
    if report_is_symlink:
        reasons.append("field-link preflight report must not be a symbolic link")
    if report_sha256 is None:
        reasons.append("field-link preflight report is unavailable")
        return {
            "schema_version": 1,
            "report_path": str(report_path),
            "report_sha256": None,
            "report_bytes": report_bytes,
            "require_ready": require_ready,
            "hardware_link_ready": False,
            "summary": None,
            "checks": checks,
            "reasons": reasons,
            "gate_passed": False,
        }
    try:
        # ``utf-8-sig`` accepts ordinary UTF-8 and the BOM emitted by several
        # Windows editors/exporters.  The byte hash above still binds the
        # exact original file; accepting the marker does not weaken provenance.
        raw = json.loads(
            report_path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        reasons.append(f"field-link preflight report cannot be loaded: {exc}")
        return {
            "schema_version": 1,
            "report_path": str(report_path),
            "report_sha256": report_sha256,
            "report_bytes": report_bytes,
            "require_ready": require_ready,
            "hardware_link_ready": False,
            "summary": None,
            "checks": checks,
            "reasons": reasons,
            "gate_passed": False,
        }
    if not isinstance(raw, Mapping):
        reasons.append("field-link preflight report must contain a JSON object")
        return {
            "schema_version": 1,
            "report_path": str(report_path),
            "report_sha256": report_sha256,
            "report_bytes": report_bytes,
            "require_ready": require_ready,
            "hardware_link_ready": False,
            "summary": None,
            "checks": checks,
            "reasons": reasons,
            "gate_passed": False,
        }

    if raw.get("schema_version") != FIELD_LINK_REPORT_SCHEMA_VERSION:
        reasons.append("unsupported field-link preflight schema_version")
    summary = raw.get("summary")
    if not isinstance(summary, Mapping):
        reasons.append("field-link preflight summary must be an object")
        summary = {}
    all_passed = summary.get("all_passed")
    hardware_ready = summary.get("hardware_link_ready")
    if type(all_passed) is not bool:
        reasons.append("field-link preflight summary all_passed must be a boolean")
    if type(hardware_ready) is not bool:
        reasons.append("field-link preflight summary hardware_link_ready must be a boolean")
    if type(all_passed) is bool and type(hardware_ready) is bool:
        if hardware_ready != all_passed:
            reasons.append("hardware_link_ready must match all_passed")
        checks["summary_consistent"] = hardware_ready == all_passed
    else:
        checks["summary_consistent"] = False

    mode = raw.get("mode")
    if mode not in (None, "watch"):
        reasons.append("unsupported field-link preflight mode")

    # A report can be structurally valid and hash-bound while still being too
    # old to describe the link used by a later acceptance run.  Freshness is
    # opt-in so historical fixtures remain readable, but callers that bind a
    # live field report can require a bounded observation window.  A watch
    # report describes a time interval, therefore its finish timestamp is the
    # authoritative observation time; one-shot reports use their generation
    # timestamp.
    report_timestamp_epoch_s: float | None = None
    report_age_s: float | None = None
    if max_age_s is not None:
        timestamp_key = "finished_at_epoch_s" if mode == "watch" else "generated_at_epoch_s"
        timestamp_candidate = raw.get(timestamp_key)
        freshness_passed = False
        if not _is_finite_number(timestamp_candidate):
            reasons.append(
                "field-link preflight report timestamp is missing or invalid "
                "for freshness validation"
            )
        else:
            report_timestamp_epoch_s = float(timestamp_candidate)
            report_age_s = time.time() - report_timestamp_epoch_s
            if report_age_s < 0.0:
                reasons.append("field-link preflight report timestamp is in the future")
            elif report_age_s > max_age_s:
                reasons.append(
                    "field-link preflight report is stale: age "
                    f"{report_age_s:.3f}s exceeds max {max_age_s:.3f}s"
                )
            else:
                freshness_passed = True
        checks["freshness"] = {
            "required": True,
            "timestamp_key": timestamp_key,
            "timestamp_epoch_s": report_timestamp_epoch_s,
            "age_s": None if report_age_s is None else round(report_age_s, 6),
            "max_age_s": max_age_s,
            "passed": freshness_passed,
        }

    if "discovery" in raw:
        discovery_result = verify_field_link_discovery(raw.get("discovery"))
        checks["discovery"] = discovery_result
        if not discovery_result["gate_passed"]:
            reasons.extend(f"discovery: {reason}" for reason in discovery_result["reasons"])

    # The optional Windows interface snapshot is a separate read-only
    # diagnostic.  When a producer binds it, reopen the exact file and
    # compare every compact field with the freshly verified source.  This
    # keeps a copied/tampered selection record from being presented as the
    # interface that carried the endpoint probes.
    if "interface_diagnostic" in raw:
        binding = raw.get("interface_diagnostic")
        interface_result: dict[str, Any] | None = None
        if not isinstance(binding, Mapping):
            reasons.append("field-link interface_diagnostic binding must be an object")
            checks["interface_diagnostic"] = {"gate_passed": False, "reasons": reasons[-1:]}
        else:
            binding_path_value = binding.get("report_path")
            if not isinstance(binding_path_value, str) or not binding_path_value.strip():
                reasons.append("field-link interface_diagnostic.report_path is missing or empty")
            else:
                binding_path = Path(binding_path_value).expanduser()
                if not binding_path.is_absolute():
                    binding_path = report_path.parent / binding_path
                target = raw.get("target")
                expected_cidr = (
                    target.get("expected_source_cidr")
                    if isinstance(target, Mapping)
                    else summary.get("route_source_cidr")
                )
                if expected_cidr is not None and not isinstance(expected_cidr, str):
                    reasons.append(
                        "field-link target expected_source_cidr must be text when binding "
                        "an interface diagnostic"
                    )
                    expected_cidr = None
                expected_alias = binding.get("interface_alias")
                if expected_alias is not None and not isinstance(expected_alias, str):
                    reasons.append(
                        "field-link interface_diagnostic.interface_alias must be text or null"
                    )
                    expected_alias = None
                interface_result = verify_field_link_interface_diagnostic(
                    binding_path,
                    expected_source_cidr=expected_cidr,
                    expected_interface_alias=expected_alias,
                )
                checks["interface_diagnostic"] = interface_result
                if not interface_result.get("gate_passed"):
                    reasons.extend(
                        f"interface_diagnostic: {reason}"
                        for reason in interface_result.get("reasons", [])
                    )
                if binding.get("required") is not True:
                    reasons.append("field-link interface_diagnostic.required must be true")
                if binding.get("verified") is not True:
                    reasons.append("field-link interface_diagnostic.verified must be true")
                if binding.get("read_only") is not True:
                    reasons.append("field-link interface_diagnostic.read_only must be true")
                if binding.get("report_path"):
                    resolved_binding_path = str(Path(binding_path).resolve(strict=False))
                    if interface_result.get("report_path") != resolved_binding_path:
                        reasons.append(
                            "field-link interface_diagnostic.report_path does not resolve "
                            "to the verified source"
                        )
                for binding_key, result_key in (
                    ("report_sha256", "report_sha256"),
                    ("report_bytes", "report_bytes"),
                    ("schema_version", "diagnostic_schema_version"),
                    ("expected_source_cidr", "expected_source_cidr"),
                    ("normalized_source_cidr", "normalized_source_cidr"),
                ):
                    if binding.get(binding_key) != interface_result.get(result_key):
                        reasons.append(
                            f"field-link interface_diagnostic.{binding_key} does not match source"
                        )
                selection = interface_result.get("selection")
                if not isinstance(selection, Mapping):
                    reasons.append("field-link interface diagnostic selection is unavailable")
                else:
                    if binding.get("selection_status") != selection.get("status"):
                        reasons.append(
                            "field-link interface_diagnostic.selection_status does not match source"
                        )
                    if binding.get("selection_candidate_count") != selection.get("candidate_count"):
                        reasons.append(
                            "field-link interface_diagnostic.selection_candidate_count does not "
                            "match source"
                        )
                    if binding.get("candidate_aliases") != selection.get("candidate_aliases"):
                        reasons.append(
                            "field-link interface_diagnostic.candidate_aliases do not match source"
                        )

    # A workstation address receipt is optional because an aircraft network
    # may be provisioned outside this helper.  When present, however, it must
    # be a successful apply document: plan/verify/what-if records cannot be
    # promoted into link-readiness provenance.  Freshness is evaluated at the
    # parent preflight observation time, so archived verification remains
    # reproducible while still proving that address setup preceded the probe
    # by only the bounded interval recorded here.
    if "address_state" in raw:
        binding = raw.get("address_state")
        address_result: dict[str, Any] | None = None
        binding_keys = {
            "required",
            "report_path",
            "report_sha256",
            "report_bytes",
            "schema",
            "schema_version",
            "interface_alias",
            "requested_address",
            "requested_prefix_length",
            "mode",
            "application_succeeded",
            "max_age_s_at_preflight",
            "verified",
        }
        if not isinstance(binding, Mapping) or set(binding) != binding_keys:
            reasons.append("field-link address_state binding must use the closed v1 shape")
            checks["address_state"] = {"gate_passed": False, "reasons": reasons[-1:]}
        else:
            binding_path_value = binding.get("report_path")
            expected_alias = binding.get("interface_alias")
            expected_address = binding.get("requested_address")
            expected_prefix = binding.get("requested_prefix_length")
            binding_max_age = binding.get("max_age_s_at_preflight")
            binding_values_valid = (
                isinstance(binding_path_value, str)
                and bool(binding_path_value.strip())
                and isinstance(expected_alias, str)
                and bool(expected_alias.strip())
                and isinstance(expected_address, str)
                and bool(expected_address.strip())
                and type(expected_prefix) is int
                and 1 <= expected_prefix <= 32
                and isinstance(binding_max_age, (int, float))
                and not isinstance(binding_max_age, bool)
                and math.isfinite(float(binding_max_age))
                and 0.0 <= float(binding_max_age) <= MAX_FIELD_LINK_ADDRESS_BINDING_AGE_S
            )
            if not binding_values_valid:
                reasons.append("field-link address_state binding values are invalid")
                checks["address_state"] = {
                    "gate_passed": False,
                    "reasons": reasons[-1:],
                }
            else:
                binding_path = Path(binding_path_value).expanduser()
                if not binding_path.is_absolute():
                    binding_path = report_path.parent / binding_path
                if binding_path.resolve(strict=False) == report_path.resolve(strict=False):
                    reasons.append("field-link address_state must not reference its parent report")
                timestamp_key = "finished_at_epoch_s" if mode == "watch" else "generated_at_epoch_s"
                parent_timestamp = raw.get(timestamp_key)
                reference_time: datetime | None = None
                if _is_finite_number(parent_timestamp):
                    try:
                        reference_time = datetime.fromtimestamp(
                            float(parent_timestamp), tz=timezone.utc
                        )
                    except (OverflowError, OSError, ValueError):
                        reference_time = None
                if reference_time is None:
                    reasons.append(
                        "field-link preflight timestamp is required for address_state binding"
                    )
                try:
                    address_result = verify_field_link_address_state(
                        binding_path,
                        require_applied=True,
                        expected_interface_alias=expected_alias,
                        expected_address=expected_address,
                        expected_prefix_length=expected_prefix,
                        max_age_s=float(binding_max_age),
                        now_utc=reference_time,
                    )
                except ValueError as exc:
                    address_result = {
                        "gate_passed": False,
                        "reasons": [str(exc)],
                    }
                checks["address_state"] = address_result
                if not address_result.get("gate_passed"):
                    reasons.extend(
                        f"address_state: {reason}" for reason in address_result.get("reasons", [])
                    )
                if binding.get("required") is not True:
                    reasons.append("field-link address_state.required must be true")
                if binding.get("verified") is not True:
                    reasons.append("field-link address_state.verified must be true")
                if binding.get("mode") != "apply":
                    reasons.append("field-link address_state.mode must be apply")
                if binding.get("application_succeeded") is not True:
                    reasons.append("field-link address_state.application_succeeded must be true")
                for binding_key, result_key in (
                    ("report_sha256", "report_sha256"),
                    ("report_bytes", "report_bytes"),
                    ("schema", "document_schema"),
                    ("schema_version", "schema_version"),
                    ("interface_alias", "interface_alias"),
                    ("requested_address", "requested_address"),
                    ("requested_prefix_length", "requested_prefix_length"),
                    ("mode", "mode"),
                    ("application_succeeded", "application_succeeded"),
                ):
                    if binding.get(binding_key) != address_result.get(result_key):
                        reasons.append(
                            f"field-link address_state.{binding_key} does not match source"
                        )
                target = raw.get("target")
                expected_cidr = (
                    target.get("expected_source_cidr") if isinstance(target, Mapping) else None
                )
                if isinstance(expected_cidr, str):
                    try:
                        if ipaddress.ip_address(expected_address) not in ipaddress.ip_network(
                            expected_cidr, strict=False
                        ):
                            reasons.append(
                                "field-link address_state requested address is outside the "
                                "preflight expected source CIDR"
                            )
                    except ValueError:
                        reasons.append(
                            "field-link address_state cannot be matched to the expected source CIDR"
                        )
                else:
                    reasons.append(
                        "field-link address_state requires a parent expected source CIDR"
                    )
                if "interface_diagnostic" in raw:
                    interface_binding = raw.get("interface_diagnostic")
                    if (
                        isinstance(interface_binding, Mapping)
                        and interface_binding.get("interface_alias") != expected_alias
                    ):
                        reasons.append(
                            "field-link address_state and interface_diagnostic aliases differ"
                        )

    # A bounded watch that stops on its first failure deliberately leaves the
    # aggregate route result unknown.  ``None`` is the only honest value until
    # every requested sample has been collected; treating it as ``False``
    # would lose the distinction between an observed bad route and an
    # unobserved remainder of the window.  One-shot reports and complete
    # watches still require a boolean whenever route checks were requested.
    partial_watch = False
    if mode == "watch":
        requested_candidate = summary.get("samples_requested")
        collected_candidate = summary.get("samples_collected")
        partial_watch = (
            type(requested_candidate) is int
            and not isinstance(requested_candidate, bool)
            and requested_candidate > 0
            and type(collected_candidate) is int
            and not isinstance(collected_candidate, bool)
            and 0 <= collected_candidate < requested_candidate
        )

    route_requested = summary.get("route_checks_requested", False)
    if type(route_requested) is not bool:
        reasons.append("field-link route_checks_requested must be a boolean")
        route_requested = False
    route_passed = summary.get("route_checks_passed")
    if (
        route_requested
        and type(route_passed) is not bool
        and not (partial_watch and route_passed is None)
    ):
        reasons.append("field-link route_checks_passed must be a boolean when requested")
    elif not route_requested and route_passed not in (None, False):
        reasons.append(
            "field-link route_checks_passed must be null when route checks are not requested"
        )
    route_cidr = summary.get("route_source_cidr")
    if route_cidr is not None:
        if not isinstance(route_cidr, str) or not route_cidr.strip():
            reasons.append("field-link route_source_cidr must be a non-empty IPv4 CIDR or null")
        else:
            try:
                parsed_route_cidr = ipaddress.ip_network(route_cidr.strip(), strict=False)
                if parsed_route_cidr.version != 4:
                    raise ValueError("route_source_cidr must be an IPv4 CIDR")
            except ValueError as exc:
                reasons.append(str(exc))

    def valid_probe_list(value: object) -> bool:
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(probe, Mapping) and type(probe.get("ok")) is bool for probe in value)
        )

    def valid_route_probe_list(value: object) -> bool:
        return (
            isinstance(value, list)
            and bool(value)
            and all(
                isinstance(probe, Mapping)
                and type(probe.get("ok")) is bool
                and probe.get("transport") == "route"
                and (
                    probe.get("source_address") is None
                    or isinstance(probe.get("source_address"), str)
                )
                for probe in value
            )
        )

    def summary_matches_probe_list(
        candidate_summary: object,
        candidate_probes: Sequence[Mapping[str, Any]],
        candidate_route_probes: Sequence[Mapping[str, Any]] | None = None,
    ) -> bool:
        """Return whether a saved summary agrees with its probe outcomes."""

        if not isinstance(candidate_summary, Mapping):
            return False
        candidate_all_passed = candidate_summary.get("all_passed")
        candidate_hardware_ready = candidate_summary.get("hardware_link_ready")
        if type(candidate_all_passed) is not bool or type(candidate_hardware_ready) is not bool:
            return False
        configuration_valid = candidate_summary.get("configuration_valid", True)
        if type(configuration_valid) is not bool:
            return False
        candidate_route_requested = candidate_summary.get("route_checks_requested", False)
        if type(candidate_route_requested) is not bool:
            return False
        candidate_route_passed = candidate_summary.get("route_checks_passed")
        if candidate_route_requested:
            if type(candidate_route_passed) is not bool or not candidate_route_probes:
                return False
            route_expected = all(bool(probe.get("ok")) for probe in candidate_route_probes)
            if candidate_route_passed != route_expected:
                return False

            # New reports expose the source address selected by the operating
            # system for every passive route probe.  Keep this optional so
            # historical v1 reports remain readable, but if it is present it
            # must be a lossless, endpoint-keyed projection of route_probes.
            route_sources = candidate_summary.get("route_source_addresses")
            if route_sources is not None:
                if not isinstance(route_sources, Mapping):
                    return False
                expected_sources: dict[str, str | None] = {}
                for probe in candidate_route_probes:
                    name = probe.get("name")
                    if not isinstance(name, str) or not name or name in expected_sources:
                        return False
                    source_address = probe.get("source_address")
                    if source_address is not None and not _is_ipv4_address(source_address):
                        return False
                    expected_sources[name] = source_address
                if dict(route_sources) != expected_sources:
                    return False
        elif candidate_route_passed not in (None, False):
            return False
        expected = (
            all(bool(probe.get("ok")) for probe in candidate_probes)
            and configuration_valid
            and (not candidate_route_requested or bool(candidate_route_passed))
        )
        return candidate_all_passed == expected and candidate_hardware_ready == expected

    probes = raw.get("probes")
    route_probes = raw.get("route_probes")
    # A watch report stores probes inside each sample rather than duplicating
    # one flattened top-level list.  Keep one-shot reports strict while
    # validating every sample in the watch branch below.
    probe_shape_valid = (
        (probes is None or valid_probe_list(probes))
        if mode == "watch"
        else valid_probe_list(probes)
    )
    checks["probe_shape_valid"] = probe_shape_valid
    if not probe_shape_valid:
        reasons.append("field-link preflight probes must be a list of objects with boolean ok")
    route_probe_shape_valid = (
        (route_probes is None or not route_probes or valid_route_probe_list(route_probes))
        if mode == "watch"
        else (
            valid_route_probe_list(route_probes)
            if route_requested
            else (route_probes in (None, []) or valid_route_probe_list(route_probes))
        )
    )
    checks["route_probe_shape_valid"] = route_probe_shape_valid
    if not route_probe_shape_valid:
        reasons.append("field-link route_probes must be route records with boolean ok")
    if route_requested and mode != "watch" and not route_probe_shape_valid:
        # Keep the diagnostic below from treating a malformed route list as
        # an all-pass list when evaluating the summary invariant.
        route_probes = None

    # The summary is derived from the probe list by ``build_field_link_report``.
    # Recompute that invariant while verifying saved evidence so a hand-edited
    # ``all_passed=true`` cannot upgrade a report containing a failed endpoint.
    summary_consistency_valid = True
    if mode != "watch" and probe_shape_valid and isinstance(summary, Mapping):
        configuration_valid = summary.get("configuration_valid", True)
        if type(configuration_valid) is not bool:
            reasons.append("field-link preflight summary configuration_valid must be a boolean")
            summary_consistency_valid = False
        if not summary_matches_probe_list(
            summary,
            probes,
            route_probes if isinstance(route_probes, Sequence) else None,
        ):
            reasons.append("field-link preflight summary does not match probes")
            summary_consistency_valid = False
    checks["summary_consistency"] = summary_consistency_valid

    watch_shape_valid = True
    if mode == "watch":
        configuration = raw.get("configuration")
        samples = raw.get("samples")
        requested = summary.get("samples_requested")
        collected = summary.get("samples_collected")
        watch_shape_valid = (
            isinstance(configuration, Mapping)
            and isinstance(samples, list)
            and isinstance(requested, int)
            and not isinstance(requested, bool)
            and requested > 0
            and isinstance(collected, int)
            and not isinstance(collected, bool)
            and collected == len(samples)
            and collected <= requested
            and all(
                isinstance(sample, Mapping)
                and valid_probe_list(sample.get("probes"))
                and isinstance(sample.get("summary"), Mapping)
                and (
                    not bool(sample.get("summary", {}).get("route_checks_requested", False))
                    or valid_route_probe_list(sample.get("route_probes"))
                )
                and type(sample["summary"].get("all_passed")) is bool
                and type(sample["summary"].get("hardware_link_ready")) is bool
                and summary_matches_probe_list(
                    sample["summary"],
                    sample["probes"],
                    sample.get("route_probes")
                    if isinstance(sample.get("route_probes"), Sequence)
                    else None,
                )
                and (
                    not bool(sample["summary"]["hardware_link_ready"])
                    or (
                        bool(sample["summary"]["all_passed"])
                        and all(probe["ok"] for probe in sample["probes"])
                    )
                )
                for sample in samples
            )
        )
        if not watch_shape_valid:
            reasons.append("watch preflight sample summary is malformed")
        elif type(hardware_ready) is bool:
            sample_ready = [bool(sample["summary"]["hardware_link_ready"]) for sample in samples]
            expected_ready = collected == requested and all(sample_ready)
            if hardware_ready != expected_ready:
                reasons.append("watch hardware_link_ready does not match collected samples")
            checks["watch_samples_consistent"] = hardware_ready == expected_ready
            if (
                type(summary.get("all_samples_passed")) is bool
                and summary["all_samples_passed"] != expected_ready
            ):
                reasons.append("watch all_samples_passed does not match collected samples")
            if type(all_passed) is bool and all_passed != expected_ready:
                reasons.append("watch all_passed does not match collected samples")
            if route_requested:
                sample_route_passes = [
                    bool(sample["summary"].get("route_checks_passed")) for sample in samples
                ]
                expected_route_passed = all(sample_route_passes) if collected == requested else None
                if route_passed != expected_route_passed:
                    reasons.append("watch route_checks_passed does not match collected samples")
            if (
                isinstance(configuration, Mapping)
                and type(configuration.get("samples_requested")) is int
                and not isinstance(configuration.get("samples_requested"), bool)
                and configuration["samples_requested"] != requested
            ):
                reasons.append("watch configuration samples_requested does not match summary")
    checks["watch_shape_valid"] = watch_shape_valid

    if require_ready and hardware_ready is not True:
        reasons.append("field-link preflight hardware_link_ready is false")
    summary_snapshot = dict(summary) if isinstance(summary, Mapping) else None
    return {
        "schema_version": 1,
        "report_path": str(report_path),
        "report_sha256": report_sha256,
        "report_bytes": report_bytes,
        "require_ready": require_ready,
        "max_age_s": max_age_s,
        "report_timestamp_epoch_s": report_timestamp_epoch_s,
        "report_age_s": None if report_age_s is None else round(report_age_s, 6),
        "mode": mode,
        "hardware_link_ready": hardware_ready is True,
        "summary": summary_snapshot,
        "checks": checks,
        "reasons": reasons,
        "gate_passed": not reasons,
    }


def probe_tcp(name: str, host: str, port: int, timeout_s: float) -> EndpointProbe:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            detail = "tcp connect succeeded"
            ok = True
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        ok = False
    return EndpointProbe(
        name=name,
        transport="tcp",
        host=host,
        port=port,
        ok=ok,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        detail=detail,
    )


def _read_bounded(
    channel: socket.socket,
    *,
    timeout_s: float,
    maximum_bytes: int,
    delimiter: bytes | None = None,
) -> bytes:
    """Read a bounded response without allowing a field probe to hang.

    The field preflight is deliberately passive. This helper only consumes
    bytes already emitted by the peer and stops at the first protocol header,
    delimiter or deadline. A device that accepts TCP but stays silent is
    therefore reported as a protocol failure instead of being upgraded to a
    ready link.
    """

    if timeout_s <= 0.0 or maximum_bytes <= 0:
        raise ValueError("bounded read requires a positive timeout and byte limit")
    deadline = time.perf_counter() + timeout_s
    output = bytearray()
    while len(output) < maximum_bytes:
        remaining = deadline - time.perf_counter()
        if remaining <= 0.0:
            break
        channel.settimeout(remaining)
        try:
            chunk = channel.recv(min(512, maximum_bytes - len(output)))
        except TimeoutError:
            break
        if not chunk:
            break
        output.extend(chunk)
        if delimiter is not None and delimiter in output:
            break
    return bytes(output)


def probe_ssh_banner(name: str, host: str, port: int, timeout_s: float) -> EndpointProbe:
    """Passively verify that a TCP endpoint speaks SSH identification."""

    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as channel:
            payload = _read_bounded(
                channel,
                timeout_s=timeout_s,
                maximum_bytes=256,
                delimiter=None,
            )
        lines = payload.splitlines()
        decoded_lines = [line.decode("ascii", errors="replace").strip() for line in lines]
        first_line = next(
            (line for line in decoded_lines if line.startswith(("SSH-2.0-", "SSH-1.99-"))),
            "",
        )
        ok = bool(first_line)
        detail = (
            f"ssh banner received: {first_line[:128]}"
            if ok
            else "tcp endpoint did not emit an SSH identification string"
        )
    except (OSError, OverflowError, TypeError, ValueError, UnicodeError) as exc:
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    return EndpointProbe(
        name=name,
        transport="ssh",
        host=host,
        port=port,
        ok=ok,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        detail=detail,
    )


def _find_mavlink_frame(payload: bytes) -> tuple[int, int] | None:
    """Return (version, offset) when a complete MAVLink v1/v2 frame is present.

    This is a structural check only: CRC/message semantics are owned by the
    MAVLink provider. The preflight stays dependency-free and does not send a
    heartbeat or stream request just to identify a port.
    """

    for offset, marker in enumerate(payload):
        if marker == 0xFE and offset + 2 <= len(payload):
            frame_length = int(payload[offset + 1]) + 8
            if len(payload) - offset >= frame_length:
                return 1, offset
        elif marker == 0xFD and offset + 2 <= len(payload):
            frame_length = int(payload[offset + 1]) + 12
            if len(payload) - offset >= frame_length:
                return 2, offset
    return None


def probe_mavlink_tcp(name: str, host: str, port: int, timeout_s: float) -> EndpointProbe:
    """Passively detect one complete MAVLink v1/v2 frame on a TCP endpoint."""

    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as channel:
            payload = _read_bounded(channel, timeout_s=timeout_s, maximum_bytes=4096)
        frame = _find_mavlink_frame(payload)
        ok = frame is not None
        detail = (
            f"MAVLink v{frame[0]} frame header detected at byte {frame[1]}"
            if frame is not None
            else "tcp endpoint emitted no complete MAVLink v1/v2 frame"
        )
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    return EndpointProbe(
        name=name,
        transport="mavlink",
        host=host,
        port=port,
        ok=ok,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        detail=detail,
    )


_RTSP_STATUS_RE = re.compile(r"^RTSP/(?P<version>\d+\.\d+)\s+(?P<status>\d{3})(?:\s|$)")


def probe_rtsp_options(name: str, host: str, port: int, timeout_s: float) -> EndpointProbe:
    """Send a read-only RTSP OPTIONS request and validate the status line."""

    started = time.perf_counter()
    request = b"OPTIONS * RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: MultiDetect-FieldPreflight/1\r\n\r\n"
    try:
        with socket.create_connection((host, port), timeout=timeout_s) as channel:
            channel.sendall(request)
            payload = _read_bounded(
                channel,
                timeout_s=timeout_s,
                maximum_bytes=4096,
                delimiter=b"\r\n\r\n",
            )
        first_line = payload.split(b"\r\n", 1)[0].decode("ascii", errors="replace").strip()
        match = _RTSP_STATUS_RE.match(first_line)
        status = int(match.group("status")) if match is not None else None
        ok = status is not None and 200 <= status <= 599
        if status in {401, 403}:
            detail = f"RTSP status {status} (service reachable; authentication required)"
        elif ok:
            detail = f"RTSP status {status}"
        else:
            detail = "tcp endpoint did not emit a valid RTSP status line"
    except (OSError, OverflowError, TypeError, ValueError, UnicodeError) as exc:
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    return EndpointProbe(
        name=name,
        transport="rtsp",
        host=host,
        port=port,
        ok=ok,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        detail=detail,
    )


def _discovery_network(cidr: str, max_hosts: int) -> tuple[ipaddress.IPv4Network, list[str]]:
    """Validate an explicit IPv4 discovery scope and enumerate its hosts.

    Discovery is intentionally opt-in and bounded.  Requiring an explicit
    CIDR prevents a desktop with several interfaces from silently scanning an
    unrelated network, while the host limit keeps a typo such as ``0.0.0.0/0``
    from turning a field preflight into an unbounded sweep.
    """

    if not isinstance(cidr, str) or not cidr.strip():
        raise ValueError("discover_cidr must be a non-empty IPv4 CIDR")
    if isinstance(max_hosts, bool) or not isinstance(max_hosts, int) or max_hosts <= 0:
        raise ValueError("discover_max_hosts must be a positive integer")
    try:
        network = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError as exc:
        raise ValueError(f"invalid discovery CIDR: {cidr!r}") from exc
    if network.version != 4:
        raise ValueError("discovery currently accepts IPv4 CIDR only")
    hosts = [str(address) for address in network.hosts()]
    # ``IPv4Network.hosts`` yields one address for /31 and /32, so the bound is
    # useful even for point-to-point aircraft links.
    if len(hosts) > max_hosts:
        raise ValueError(
            f"discovery CIDR {network} contains {len(hosts)} hosts; "
            f"reduce the scope or raise discover_max_hosts (current {max_hosts})"
        )
    return network, hosts


def _probe_discovery_host(
    host: str,
    *,
    jetson_port: int,
    gr01_port: int,
    rtsp_port: int,
    timeout_s: float,
    protocol_checks: bool,
) -> dict[str, Any]:
    """Probe one candidate host using only the field preflight read-only checks."""

    probe_ssh = probe_ssh_banner if protocol_checks else probe_tcp
    probe_mavlink = probe_mavlink_tcp if protocol_checks else probe_tcp
    probe_rtsp = probe_rtsp_options if protocol_checks else probe_tcp
    probes = (
        probe_ssh("ssh", host, jetson_port, timeout_s),
        probe_mavlink("mavlink", host, gr01_port, timeout_s),
        probe_rtsp("rtsp", host, rtsp_port, timeout_s),
    )
    services = {probe.name: asdict(probe) for probe in probes}
    return {
        "host": host,
        "responsive": any(probe.ok for probe in probes),
        "services": services,
    }


def discover_field_link_hosts(
    *,
    cidr: str,
    jetson_port: int = 22,
    gr01_port: int = 5760,
    rtsp_port: int = 554,
    timeout_s: float = 0.25,
    protocol_checks: bool = True,
    max_hosts: int = DEFAULT_DISCOVERY_MAX_HOSTS,
    workers: int = DEFAULT_DISCOVERY_WORKERS,
) -> dict[str, Any]:
    """Discover likely field-link hosts inside one explicit local IPv4 CIDR.

    The result is diagnostic only.  A discovered host is never substituted for
    ``jetson_host``/``gr01_host``/``camera_host`` and cannot make
    ``hardware_link_ready`` true.  Callers must run the normal, hash-bound
    preflight against explicitly chosen endpoints afterwards.
    """

    if not math.isfinite(timeout_s) or timeout_s <= 0.0:
        raise ValueError("discover_timeout_s must be finite and greater than zero")
    if type(protocol_checks) is not bool:
        raise ValueError("discover_protocol_checks must be a boolean")
    for name, port in (
        ("discover_jetson_port", jetson_port),
        ("discover_gr01_port", gr01_port),
        ("discover_rtsp_port", rtsp_port),
    ):
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{name} must be an integer in [1, 65535]")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
        raise ValueError("discover_workers must be a positive integer")
    network, hosts = _discovery_network(cidr, max_hosts)
    started = time.perf_counter()
    worker_count = min(workers, max(1, len(hosts)))
    results: list[dict[str, Any]] = []
    # ``executor.map`` preserves input order, keeping evidence deterministic
    # even though socket probes run concurrently.
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                _probe_discovery_host,
                host,
                jetson_port=jetson_port,
                gr01_port=gr01_port,
                rtsp_port=rtsp_port,
                timeout_s=timeout_s,
                protocol_checks=protocol_checks,
            )
            for host in hosts
        ]
        for future in futures:
            results.append(future.result())
    responsive = [item for item in results if item["responsive"]]
    service_hits = {
        service: sum(bool(item["services"].get(service, {}).get("ok")) for item in results)
        for service in ("ssh", "mavlink", "rtsp")
    }
    service_candidates = {
        service: [
            item["host"] for item in responsive if bool(item["services"].get(service, {}).get("ok"))
        ]
        for service in ("ssh", "mavlink", "rtsp")
    }
    return {
        "schema_version": FIELD_LINK_DISCOVERY_SCHEMA_VERSION,
        "mode": "discovery",
        "cidr": str(network),
        "ports": {
            "ssh": jetson_port,
            "mavlink": gr01_port,
            "rtsp": rtsp_port,
        },
        "timeout_s": timeout_s,
        "protocol_checks": protocol_checks,
        "max_hosts": max_hosts,
        "workers": worker_count,
        "scanned_hosts": len(hosts),
        "responsive_hosts": len(responsive),
        "service_hits": service_hits,
        "service_candidates": service_candidates,
        "hosts": responsive,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "notes": [
            "Discovery is opt-in, scoped to the explicit IPv4 CIDR, and read-only.",
            (
                "SSH/MAVLink/RTSP probes identify services but do not log in, request "
                "streams, or send flight commands."
            ),
            (
                "Use a responsive host to rerun the normal explicit field-link preflight; "
                "discovery never marks hardware_link_ready."
            ),
        ],
    }


def probe_udp_bind(name: str, host: str, port: int) -> EndpointProbe:
    started = time.perf_counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
        detail = "local udp bind succeeded"
        ok = True
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        ok = False
    finally:
        sock.close()
    return EndpointProbe(
        name=name,
        transport="udp-bind",
        host=host,
        port=port,
        ok=ok,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        detail=detail,
    )


def probe_local_route(
    name: str,
    host: str,
    port: int,
    *,
    expected_source_cidr: str | None = None,
) -> RouteProbe:
    """Resolve the local interface selected for a remote endpoint.

    The operating-system route table is queried through a UDP ``connect``;
    no payload is sent and no remote service is contacted.  When
    ``expected_source_cidr`` is supplied, the selected source address must
    belong to that explicit IPv4 network.  This catches a common workstation
    migration failure where a VPN/TAP route silently wins over the aircraft
    Ethernet interface while a superficial TCP probe is still attempted.
    """

    started = time.perf_counter()
    source_address: str | None = None
    try:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer in [1, 65535]")
        expected_network: ipaddress.IPv4Network | None = None
        if expected_source_cidr is not None:
            if not isinstance(expected_source_cidr, str) or not expected_source_cidr.strip():
                raise ValueError("expected_source_cidr must be a non-empty IPv4 CIDR")
            parsed = ipaddress.ip_network(expected_source_cidr.strip(), strict=False)
            if parsed.version != 4:
                raise ValueError("expected_source_cidr must be an IPv4 CIDR")
            expected_network = parsed
        addresses = socket.getaddrinfo(
            host.strip(),
            port,
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )
        if not addresses:
            raise OSError("no IPv4 route candidate returned")
        destination = addresses[0][4]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
            # UDP connect performs route selection locally; unlike sendto it
            # does not put an application datagram on the wire.
            channel.connect(destination)
            source_address = str(channel.getsockname()[0])
        if not source_address or source_address == "0.0.0.0":
            raise OSError("operating system did not select a usable source address")
        if expected_network is not None:
            source_ip = ipaddress.ip_address(source_address)
            if source_ip not in expected_network:
                return RouteProbe(
                    name=name,
                    transport="route",
                    host=host,
                    port=port,
                    ok=False,
                    source_address=source_address,
                    elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
                    detail=(
                        f"route selected source {source_address}, outside expected "
                        f"{expected_network}"
                    ),
                )
        detail = f"route selected source {source_address}"
        if expected_network is not None:
            detail += f" within expected {expected_network}"
        return RouteProbe(
            name=name,
            transport="route",
            host=host,
            port=port,
            ok=True,
            source_address=source_address,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
            detail=detail,
        )
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        return RouteProbe(
            name=name,
            transport="route",
            host=host,
            port=port,
            ok=False,
            source_address=source_address,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
            detail=f"{type(exc).__name__}: {exc}",
        )


def build_field_link_report(
    *,
    jetson_host: str,
    gr01_host: str,
    camera_host: str,
    jetson_port: int = 22,
    gr01_port: int = 5760,
    rtsp_port: int = 554,
    metadata_port: int = 14580,
    qgc_port: int = 14581,
    timeout_s: float = 2.0,
    protocol_checks: bool = False,
    expected_source_cidr: str | None = None,
    require_route: bool = False,
) -> dict[str, Any]:
    configuration_errors: list[str] = []
    for name, port in (
        ("jetson_port", jetson_port),
        ("gr01_port", gr01_port),
        ("rtsp_port", rtsp_port),
        ("metadata_port", metadata_port),
        ("qgc_port", qgc_port),
    ):
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            configuration_errors.append(f"{name} must be an integer in [0, 65535]")
    # QGC owns the local receive socket while the Jetson side owns the
    # selection/confirmation socket.  A sequential bind probe would let the
    # same fixed port pass because each probe closes before the next one; fail
    # closed at the configuration layer instead.  Port 0 is intentionally
    # allowed for unit tests/ephemeral probes and is not treated as a fixed
    # production collision.
    if (
        isinstance(metadata_port, int)
        and not isinstance(metadata_port, bool)
        and isinstance(qgc_port, int)
        and not isinstance(qgc_port, bool)
        and metadata_port > 0
        and qgc_port > 0
        and metadata_port == qgc_port
    ):
        configuration_errors.append(
            f"metadata_port and qgc_port must differ (both are {metadata_port})"
        )
    if type(protocol_checks) is not bool:
        configuration_errors.append("protocol_checks must be a boolean")
        protocol_checks = False
    if expected_source_cidr is not None and (
        not isinstance(expected_source_cidr, str) or not expected_source_cidr.strip()
    ):
        configuration_errors.append("expected_source_cidr must be a non-empty IPv4 CIDR or null")
        expected_source_cidr = None
    elif expected_source_cidr is not None:
        try:
            parsed_source_network = ipaddress.ip_network(expected_source_cidr.strip(), strict=False)
            if parsed_source_network.version != 4:
                raise ValueError("expected_source_cidr must be an IPv4 CIDR")
            expected_source_cidr = str(parsed_source_network)
        except ValueError as exc:
            configuration_errors.append(str(exc))
            expected_source_cidr = None
    if type(require_route) is not bool:
        configuration_errors.append("require_route must be a boolean")
        require_route = False
    route_checks_requested = require_route or expected_source_cidr is not None
    probes = [
        probe_tcp("jetson-ssh", jetson_host, jetson_port, timeout_s),
        probe_tcp("gr01-mavlink", gr01_host, gr01_port, timeout_s),
        probe_tcp("camera-rtsp", camera_host, rtsp_port, timeout_s),
        probe_udp_bind("qgc-metadata-local", "127.0.0.1", qgc_port),
        probe_udp_bind("jetson-metadata-local-check", "127.0.0.1", metadata_port),
    ]
    if protocol_checks:
        probes.extend(
            (
                probe_ssh_banner("jetson-ssh-protocol", jetson_host, jetson_port, timeout_s),
                probe_mavlink_tcp("gr01-mavlink-protocol", gr01_host, gr01_port, timeout_s),
                probe_rtsp_options("camera-rtsp-protocol", camera_host, rtsp_port, timeout_s),
            )
        )
    route_probes = (
        [
            probe_local_route(
                "jetson-ssh-route",
                jetson_host,
                jetson_port,
                expected_source_cidr=expected_source_cidr,
            ),
            probe_local_route(
                "gr01-mavlink-route",
                gr01_host,
                gr01_port,
                expected_source_cidr=expected_source_cidr,
            ),
            probe_local_route(
                "camera-rtsp-route",
                camera_host,
                rtsp_port,
                expected_source_cidr=expected_source_cidr,
            ),
        ]
        if route_checks_requested
        else []
    )
    tcp_probes = [probe for probe in probes if probe.transport == "tcp"]
    local_probes = [probe for probe in probes if probe.transport == "udp-bind"]
    protocol_probes = [probe for probe in probes if probe.transport in {"ssh", "mavlink", "rtsp"}]
    protocol_checks_passed = all(probe.ok for probe in protocol_probes) if protocol_checks else None
    route_checks_passed = (
        all(probe.ok for probe in route_probes) if route_checks_requested else None
    )
    # Keep the selected source address visible at summary level.  This is a
    # diagnostic projection only: it does not replace the per-probe route
    # records and it cannot turn an unreachable endpoint into a ready link.
    route_source_addresses = (
        {probe.name: probe.source_address for probe in route_probes}
        if route_checks_requested
        else None
    )
    probes_passed = all(probe.ok for probe in probes) and not configuration_errors
    hardware_link_ready = (
        all(probe.ok for probe in tcp_probes)
        and all(probe.ok for probe in local_probes)
        and (not protocol_checks or bool(protocol_checks_passed))
        and (not route_checks_requested or bool(route_checks_passed))
        and not configuration_errors
    )
    summary = {
        "tcp_reachable": all(probe.ok for probe in tcp_probes),
        "local_udp_ports_available": all(probe.ok for probe in local_probes),
        "protocol_checks_requested": protocol_checks,
        "protocol_checks_passed": protocol_checks_passed,
        "route_checks_requested": route_checks_requested,
        "route_checks_passed": route_checks_passed,
        "route_source_cidr": expected_source_cidr,
        "configuration_valid": not configuration_errors,
        "configuration_errors": configuration_errors,
        "all_passed": probes_passed and (not route_checks_requested or bool(route_checks_passed)),
        "hardware_link_ready": hardware_link_ready,
    }
    if route_checks_requested:
        summary["route_source_addresses"] = route_source_addresses
    return {
        "schema_version": 1,
        "generated_at_epoch_s": time.time(),
        "target": {
            "jetson_host": jetson_host,
            "gr01_host": gr01_host,
            "camera_host": camera_host,
            "jetson_port": jetson_port,
            "gr01_port": gr01_port,
            "rtsp_port": rtsp_port,
            "metadata_port": metadata_port,
            "qgc_port": qgc_port,
            "expected_source_cidr": expected_source_cidr,
            "require_route": require_route,
        },
        "probes": [asdict(probe) for probe in probes],
        "route_probes": [asdict(probe) for probe in route_probes],
        "summary": summary,
        "notes": [
            (
                "TCP probes verify reachability only; they do not authenticate "
                "or send flight commands."
            ),
            (
                "UDP bind probes verify local port availability; they do not claim "
                "a remote UDP peer is present."
            ),
            (
                "Protocol checks are optional and read-only: SSH reads the server banner, "
                "MAVLink passively looks for one complete frame, and RTSP sends OPTIONS only."
            ),
            (
                "The metadata and QGC local UDP ports must be distinct; this is "
                "checked separately from sequential bind probes."
            ),
            (
                "Route checks are opt-in and passive: UDP connect only asks the local "
                "OS which source interface it would use; --expected-source-cidr also "
                "checks that source against the requested aircraft network."
            ),
            "A passing preflight does not prove GNSS fix, sensor freshness, or actuator readiness.",
        ],
    }


def watch_field_link(
    *,
    jetson_host: str,
    gr01_host: str,
    camera_host: str,
    jetson_port: int = 22,
    gr01_port: int = 5760,
    rtsp_port: int = 554,
    metadata_port: int = 14580,
    qgc_port: int = 14581,
    timeout_s: float = 2.0,
    protocol_checks: bool = False,
    expected_source_cidr: str | None = None,
    require_route: bool = False,
    samples: int = 1,
    interval_s: float = 1.0,
    stop_on_failure: bool = False,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run bounded, read-only repeated field-link preflight samples.

    A single successful TCP connection is not enough evidence for a field
    session: an aircraft Ethernet link can flap while all services remain
    configured.  This helper repeats the same preflight contract without
    logging in, requesting MAVLink streams, starting video, or sending
    control data.  The aggregate is strict: every requested sample must be
    collected and every sample must pass for ``hardware_link_ready`` to be
    true.  ``stop_on_failure`` is useful during bring-up when the first
    failing sample should return immediately while preserving the failure
    index in the evidence.
    """

    if isinstance(samples, bool) or not isinstance(samples, int) or samples <= 0:
        raise ValueError("samples must be a positive integer")
    if not math.isfinite(interval_s) or interval_s < 0.0:
        raise ValueError("interval_s must be finite and non-negative")
    if type(stop_on_failure) is not bool:
        raise ValueError("stop_on_failure must be a boolean")
    if type(protocol_checks) is not bool:
        raise ValueError("protocol_checks must be a boolean")
    if expected_source_cidr is not None and not isinstance(expected_source_cidr, str):
        raise ValueError("expected_source_cidr must be a string or None")
    if type(require_route) is not bool:
        raise ValueError("require_route must be a boolean")
    if not callable(clock) or not callable(sleep):
        raise ValueError("clock and sleep must be callable")

    started_at = float(clock())
    if not math.isfinite(started_at):
        raise ValueError("clock must return a finite timestamp")
    sample_reports: list[dict[str, Any]] = []
    stop_reason = "samples_complete"
    for sample_index in range(1, samples + 1):
        report = build_field_link_report(
            jetson_host=jetson_host,
            gr01_host=gr01_host,
            camera_host=camera_host,
            jetson_port=jetson_port,
            gr01_port=gr01_port,
            rtsp_port=rtsp_port,
            metadata_port=metadata_port,
            qgc_port=qgc_port,
            timeout_s=timeout_s,
            protocol_checks=protocol_checks,
            expected_source_cidr=expected_source_cidr,
            require_route=require_route,
        )
        sample = dict(report)
        sample["sample_index"] = sample_index
        observed_at = float(clock())
        if not math.isfinite(observed_at):
            raise ValueError("clock must return finite timestamps")
        sample["observed_at_epoch_s"] = observed_at
        sample_reports.append(sample)
        if not bool(report.get("summary", {}).get("hardware_link_ready")):
            if stop_on_failure:
                stop_reason = "failure"
                break
        if sample_index < samples:
            sleep(interval_s)

    sample_passes = [bool(report.get("summary", {}).get("all_passed")) for report in sample_reports]
    hardware_passes = [
        bool(report.get("summary", {}).get("hardware_link_ready")) for report in sample_reports
    ]
    route_checks_requested = require_route or expected_source_cidr is not None
    route_passes = [
        bool(report.get("summary", {}).get("route_checks_passed")) for report in sample_reports
    ]
    first_failure_index = next(
        (index for index, passed in enumerate(hardware_passes, start=1) if not passed),
        None,
    )
    max_consecutive_failures = 0
    consecutive_failures = 0
    for passed in hardware_passes:
        if passed:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            max_consecutive_failures = max(max_consecutive_failures, consecutive_failures)
    if len(sample_reports) < samples and stop_reason == "samples_complete":
        stop_reason = "incomplete"
    finished_at = float(clock())
    if not math.isfinite(finished_at):
        raise ValueError("clock must return a finite timestamp")
    collected = len(sample_reports)
    all_samples_passed = collected == samples and all(sample_passes)
    all_hardware_ready = collected == samples and all(hardware_passes)
    all_routes_passed = (
        all(route_passes) if route_checks_requested and collected == samples else None
    )
    return {
        "schema_version": 1,
        "mode": "watch",
        "configuration": {
            "jetson_host": jetson_host,
            "gr01_host": gr01_host,
            "camera_host": camera_host,
            "jetson_port": jetson_port,
            "gr01_port": gr01_port,
            "rtsp_port": rtsp_port,
            "metadata_port": metadata_port,
            "qgc_port": qgc_port,
            "timeout_s": timeout_s,
            "protocol_checks": protocol_checks,
            "expected_source_cidr": expected_source_cidr,
            "require_route": require_route,
            "samples_requested": samples,
            "interval_s": interval_s,
            "stop_on_failure": stop_on_failure,
        },
        "started_at_epoch_s": started_at,
        "finished_at_epoch_s": finished_at,
        "samples": sample_reports,
        "summary": {
            "samples_requested": samples,
            "samples_collected": collected,
            "stop_reason": stop_reason,
            "first_failure_index": first_failure_index,
            "passed_samples": sum(sample_passes),
            "failed_samples": collected - sum(sample_passes),
            "stability_ratio": round(sum(hardware_passes) / collected, 6) if collected else 0.0,
            "max_consecutive_failures": max_consecutive_failures,
            "protocol_checks_requested": protocol_checks,
            "route_checks_requested": route_checks_requested,
            "route_checks_passed": all_routes_passed,
            "route_source_cidr": expected_source_cidr,
            "all_samples_passed": all_samples_passed,
            "hardware_link_ready": all_hardware_ready,
            "all_passed": all_samples_passed,
        },
        "notes": [
            (
                "Watch mode repeats the one-shot preflight only; it is read-only and does not "
                "log in, request MAVLink streams, start RTSP video, or send flight-control data."
            ),
            (
                "hardware_link_ready requires every requested sample to pass and all samples to "
                "be collected; a partial or flapping watch stays fail-closed."
            ),
            (
                "A passing watch still does not prove GNSS fix, GPS/RANGE_STATUS freshness, "
                "camera calibration, actuator safety, HIL, REAL, or outdoor flight readiness."
            ),
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe the Multi-Detect Jetson, GR01, RTSP and local metadata link before field work."
        )
    )
    parser.add_argument("--jetson-host", default="192.168.144.20")
    parser.add_argument("--gr01-host", default="192.168.144.11")
    parser.add_argument("--camera-host", default="192.168.144.108")
    parser.add_argument("--jetson-port", type=int, default=22)
    parser.add_argument("--gr01-port", type=int, default=5760)
    parser.add_argument("--rtsp-port", type=int, default=554)
    parser.add_argument("--metadata-port", type=int, default=14580)
    parser.add_argument("--qgc-port", type=int, default=14581)
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument(
        "--protocol-checks",
        action="store_true",
        help=(
            "run optional read-only SSH-banner, passive MAVLink-frame and RTSP-OPTIONS "
            "checks in addition to TCP reachability"
        ),
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help=(
            "repeat the same read-only preflight this many times; every requested sample "
            "must pass for hardware_link_ready"
        ),
    )
    parser.add_argument(
        "--interval-s",
        type=float,
        default=1.0,
        help="delay between watch samples (seconds; default: 1.0)",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="stop a multi-sample watch at the first failed hardware-link sample",
    )
    parser.add_argument(
        "--expected-source-cidr",
        help=(
            "optional explicit IPv4 CIDR for the local source interface used to reach "
            "the three remote endpoints; enables passive route checks"
        ),
    )
    parser.add_argument(
        "--require-route",
        action="store_true",
        help=(
            "require a passive local route to every remote endpoint for readiness; "
            "combine with --expected-source-cidr to enforce the aircraft subnet"
        ),
    )
    parser.add_argument(
        "--discover-cidr",
        help=(
            "optional explicit IPv4 CIDR for a bounded read-only SSH/MAVLink/RTSP "
            "service discovery (diagnostic only; does not select endpoints)"
        ),
    )
    parser.add_argument(
        "--discover-max-hosts",
        type=int,
        default=DEFAULT_DISCOVERY_MAX_HOSTS,
        help=(
            f"maximum hosts to scan for --discover-cidr (default: {DEFAULT_DISCOVERY_MAX_HOSTS})"
        ),
    )
    parser.add_argument(
        "--discover-workers",
        type=int,
        default=DEFAULT_DISCOVERY_WORKERS,
        help=(f"maximum concurrent discovery workers (default: {DEFAULT_DISCOVERY_WORKERS})"),
    )
    parser.add_argument(
        "--discover-timeout-s",
        type=float,
        help="per-service discovery timeout (defaults to --timeout-s)",
    )
    parser.add_argument(
        "--interface-diagnostic",
        type=Path,
        help=(
            "optional read-only inspect_field_link_interfaces.ps1 JSON to hash-bind "
            "the workstation interface selection"
        ),
    )
    parser.add_argument(
        "--interface-alias",
        help=(
            "explicit interface alias used for the preflight; it binds an optional "
            "interface diagnostic and is required with --address-state"
        ),
    )
    parser.add_argument(
        "--address-state",
        type=Path,
        help=(
            "optional successful prepare_field_link.ps1 apply JSON to hash-bind; "
            "plan/verify/what-if documents are rejected"
        ),
    )
    parser.add_argument(
        "--address-state-max-age-s",
        type=float,
        default=300.0,
        help=(
            "maximum age of --address-state at the preflight observation time "
            f"(maximum/default: {MAX_FIELD_LINK_ADDRESS_BINDING_AGE_S:g})"
        ),
    )
    parser.add_argument("--expected-field-address", default="192.168.144.250")
    parser.add_argument("--expected-field-prefix-length", type=int, default=24)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--require-all", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout_s <= 0:
        parser.error("--timeout-s must be greater than zero")
    if args.samples <= 0:
        parser.error("--samples must be a positive integer")
    if not math.isfinite(args.interval_s) or args.interval_s < 0.0:
        parser.error("--interval-s must be finite and non-negative")
    if args.discover_max_hosts <= 0:
        parser.error("--discover-max-hosts must be a positive integer")
    if args.discover_workers <= 0:
        parser.error("--discover-workers must be a positive integer")
    discover_timeout_s = (
        args.timeout_s if args.discover_timeout_s is None else args.discover_timeout_s
    )
    if not math.isfinite(discover_timeout_s) or discover_timeout_s <= 0.0:
        parser.error("--discover-timeout-s must be finite and greater than zero")
    if args.interface_alias is not None and (
        args.interface_diagnostic is None and args.address_state is None
    ):
        parser.error("--interface-alias requires --interface-diagnostic or --address-state")
    if args.address_state is not None and args.interface_alias is None:
        parser.error("--address-state requires --interface-alias")
    if args.address_state is not None and args.expected_source_cidr is None:
        parser.error("--address-state requires --expected-source-cidr")
    if not math.isfinite(args.address_state_max_age_s) or not (
        0.0 <= args.address_state_max_age_s <= MAX_FIELD_LINK_ADDRESS_BINDING_AGE_S
    ):
        parser.error(
            "--address-state-max-age-s must be finite and in "
            f"[0, {MAX_FIELD_LINK_ADDRESS_BINDING_AGE_S:g}]"
        )
    if not 1 <= args.expected_field_prefix_length <= 32:
        parser.error("--expected-field-prefix-length must be in [1, 32]")
    report = (
        build_field_link_report(
            jetson_host=args.jetson_host,
            gr01_host=args.gr01_host,
            camera_host=args.camera_host,
            jetson_port=args.jetson_port,
            gr01_port=args.gr01_port,
            rtsp_port=args.rtsp_port,
            metadata_port=args.metadata_port,
            qgc_port=args.qgc_port,
            timeout_s=args.timeout_s,
            protocol_checks=args.protocol_checks,
            expected_source_cidr=args.expected_source_cidr,
            require_route=args.require_route,
        )
        if args.samples == 1 and not args.stop_on_failure
        else watch_field_link(
            jetson_host=args.jetson_host,
            gr01_host=args.gr01_host,
            camera_host=args.camera_host,
            jetson_port=args.jetson_port,
            gr01_port=args.gr01_port,
            rtsp_port=args.rtsp_port,
            metadata_port=args.metadata_port,
            qgc_port=args.qgc_port,
            timeout_s=args.timeout_s,
            protocol_checks=args.protocol_checks,
            expected_source_cidr=args.expected_source_cidr,
            require_route=args.require_route,
            samples=args.samples,
            interval_s=args.interval_s,
            stop_on_failure=args.stop_on_failure,
        )
    )
    if args.discover_cidr:
        try:
            report["discovery"] = discover_field_link_hosts(
                cidr=args.discover_cidr,
                jetson_port=args.jetson_port,
                gr01_port=args.gr01_port,
                rtsp_port=args.rtsp_port,
                timeout_s=discover_timeout_s,
                protocol_checks=args.protocol_checks,
                max_hosts=args.discover_max_hosts,
                workers=args.discover_workers,
            )
            report.setdefault("notes", []).append(
                "Discovery results are diagnostic only and never select endpoints or "
                "change hardware_link_ready."
            )
        except ValueError as exc:
            parser.error(str(exc))
    if args.interface_diagnostic is not None:
        diagnostic_path = args.interface_diagnostic.expanduser().resolve(strict=False)
        if args.out is not None and diagnostic_path == args.out.expanduser().resolve(strict=False):
            parser.error("--interface-diagnostic and --out must refer to different files")
        interface_result = verify_field_link_interface_diagnostic(
            diagnostic_path,
            expected_source_cidr=args.expected_source_cidr,
            expected_interface_alias=args.interface_alias,
        )
        if not interface_result.get("gate_passed"):
            reasons = "; ".join(str(reason) for reason in interface_result.get("reasons", []))
            parser.error(f"interface diagnostic verification failed: {reasons}")
        selection = interface_result.get("selection")
        if not isinstance(selection, Mapping):
            parser.error("interface diagnostic verification returned no selection")
        report["interface_diagnostic"] = {
            "required": True,
            "report_path": interface_result["report_path"],
            "report_sha256": interface_result["report_sha256"],
            "report_bytes": interface_result["report_bytes"],
            "schema_version": interface_result["diagnostic_schema_version"],
            "read_only": interface_result["read_only"],
            "expected_source_cidr": interface_result["expected_source_cidr"],
            "normalized_source_cidr": interface_result["normalized_source_cidr"],
            "selection_status": selection["status"],
            "selection_candidate_count": selection["candidate_count"],
            "candidate_aliases": selection["candidate_aliases"],
            "interface_alias": args.interface_alias,
            "verified": True,
        }
        report.setdefault("notes", []).append(
            "The read-only workstation interface diagnostic is hash-bound; it describes "
            "adapter selection only and never upgrades hardware_link_ready."
        )
    if args.address_state is not None:
        address_requested_path = args.address_state.expanduser()
        address_path = address_requested_path.resolve(strict=False)
        if args.out is not None and address_path == args.out.expanduser().resolve(strict=False):
            parser.error("--address-state and --out must refer to different files")
        report_mode = report.get("mode")
        timestamp_key = "finished_at_epoch_s" if report_mode == "watch" else "generated_at_epoch_s"
        report_timestamp = report.get(timestamp_key)
        if not _is_finite_number(report_timestamp):
            parser.error("field-link report has no valid observation timestamp for --address-state")
        try:
            reference_time = datetime.fromtimestamp(float(report_timestamp), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            parser.error("field-link report observation timestamp is out of range")
        try:
            address_result = verify_field_link_address_state(
                address_requested_path,
                require_applied=True,
                expected_interface_alias=args.interface_alias,
                expected_address=args.expected_field_address,
                expected_prefix_length=args.expected_field_prefix_length,
                max_age_s=args.address_state_max_age_s,
                now_utc=reference_time,
            )
        except ValueError as exc:
            parser.error(str(exc))
        if not address_result.get("gate_passed"):
            detail = "; ".join(str(reason) for reason in address_result.get("reasons", []))
            parser.error(f"field-link address state verification failed: {detail}")
        if args.expected_source_cidr is not None:
            try:
                if ipaddress.ip_address(args.expected_field_address) not in ipaddress.ip_network(
                    args.expected_source_cidr, strict=False
                ):
                    parser.error("--expected-field-address is outside --expected-source-cidr")
            except ValueError as exc:
                parser.error(f"invalid field address/source CIDR binding: {exc}")
        report["address_state"] = {
            "required": True,
            "report_path": address_result["report_path"],
            "report_sha256": address_result["report_sha256"],
            "report_bytes": address_result["report_bytes"],
            "schema": address_result["document_schema"],
            "schema_version": address_result["schema_version"],
            "interface_alias": address_result["interface_alias"],
            "requested_address": address_result["requested_address"],
            "requested_prefix_length": address_result["requested_prefix_length"],
            "mode": address_result["mode"],
            "application_succeeded": address_result["application_succeeded"],
            "max_age_s_at_preflight": args.address_state_max_age_s,
            "verified": True,
        }
        report.setdefault("notes", []).append(
            "The successful field-link address apply receipt is hash-bound and was "
            "fresh at the preflight observation time; it cannot replace endpoint probes."
        )
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if (report["summary"]["all_passed"] or not args.require_all) else 1


def verify_main(argv: Sequence[str] | None = None) -> int:
    """Verify a saved field-link report without probing or contacting devices."""

    parser = argparse.ArgumentParser(
        description=(
            "Verify a saved Multi-Detect field-link preflight report; "
            "no network probes or device logins are performed."
        )
    )
    parser.add_argument("report", type=Path, help="saved field-link preflight JSON report")
    parser.add_argument(
        "--allow-unready",
        action="store_true",
        help=(
            "validate report structure and provenance while allowing "
            "hardware_link_ready=false for diagnostics"
        ),
    )
    parser.add_argument(
        "--max-age-s",
        type=float,
        default=None,
        help=(
            "require the report observation timestamp to be no older than this many seconds; "
            "watch reports use finished_at_epoch_s"
        ),
    )
    args = parser.parse_args(argv)
    result = verify_field_link_report(
        args.report,
        require_ready=not args.allow_unready,
        max_age_s=args.max_age_s,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

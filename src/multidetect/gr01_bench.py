from __future__ import annotations

import ipaddress
import json
import math
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .autonomy.serialization import strict_json_loads
from .compat import UTC
from .domain import BoundingBox
from .operator_link import SelectionAction, TargetSelectionCommand, VideoGeometry
from .operator_transport import SelectionDeliveryTimeout

GR01_BENCH_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Gr01BenchConfig:
    minimum_round_trips: int = 100
    command_ttl_seconds: float = 3.0
    maximum_packet_loss_rate: float = 0.01
    maximum_ack_latency_p95_ms: float = 500.0
    hardware_mode: bool = False
    hardware_id: str | None = None
    expected_source_cidr: str | None = None

    def __post_init__(self) -> None:
        if type(self.hardware_mode) is not bool:
            raise ValueError("GR01 bench hardware_mode must be a boolean")
        if (
            isinstance(self.minimum_round_trips, bool)
            or not isinstance(self.minimum_round_trips, int)
            or self.minimum_round_trips <= 0
        ):
            raise ValueError("GR01 bench minimum round trips must be a positive integer")
        for name, value in (
            ("command TTL", self.command_ttl_seconds),
            ("maximum ACK latency", self.maximum_ack_latency_p95_ms),
        ):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"GR01 bench {name} must be finite and positive")
        if (
            isinstance(self.maximum_packet_loss_rate, bool)
            or not math.isfinite(self.maximum_packet_loss_rate)
            or not 0 <= self.maximum_packet_loss_rate <= 1
        ):
            raise ValueError("GR01 bench packet-loss limit must be in [0, 1]")
        if self.hardware_mode and (
            not isinstance(self.hardware_id, str) or not self.hardware_id.strip()
        ):
            raise ValueError("GR01 hardware mode requires a non-empty hardware ID")
        if not self.hardware_mode and self.hardware_id is not None:
            raise ValueError("GR01 hardware ID requires hardware mode")
        if self.hardware_mode and (
            not isinstance(self.expected_source_cidr, str) or not self.expected_source_cidr.strip()
        ):
            raise ValueError("GR01 hardware mode requires an expected source CIDR")
        if not self.hardware_mode and self.expected_source_cidr is not None:
            raise ValueError("GR01 expected source CIDR requires hardware mode")
        if self.expected_source_cidr is not None:
            try:
                network = ipaddress.ip_network(self.expected_source_cidr, strict=False)
            except ValueError as exc:
                raise ValueError("GR01 expected source CIDR is invalid") from exc
            if network.version != 4 or network.is_loopback:
                raise ValueError("GR01 expected source CIDR must be non-loopback IPv4")


def run_gr01_link_bench(
    session: Any,
    geometry: VideoGeometry,
    config: Gr01BenchConfig,
    *,
    wall_clock: Any = time.time,
    observed_at: Any = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    remote_host = str(session.remote[0])
    remote_port = int(session.remote[1])
    try:
        remote_address = ipaddress.ip_address(remote_host)
        remote_is_loopback = remote_address.is_loopback
        remote_is_ipv4 = remote_address.version == 4
    except ValueError:
        remote_is_loopback = True
        remote_is_ipv4 = False
    expected_source_cidr = (
        str(ipaddress.ip_network(config.expected_source_cidr, strict=False))
        if config.expected_source_cidr is not None
        else None
    )
    raw_source = getattr(session, "local_source_address", None)
    try:
        local_source_address = str(ipaddress.ip_address(raw_source))
    except (ValueError, TypeError):
        local_source_address = None
    source_route_verified = False
    if local_source_address is not None and expected_source_cidr is not None:
        source_route_verified = ipaddress.ip_address(local_source_address) in ipaddress.ip_network(
            expected_source_cidr
        )
    route_blocked = config.hardware_mode and not source_route_verified
    accepted_round_trips = 0
    rejected_round_trips = 0
    timed_out_round_trips = 0
    transmission_attempts = 0
    missing_ack_attempts = 0
    latencies_ms: list[float] = []
    session_id = str(uuid4())
    maximum_attempts = int(getattr(session, "maximum_attempts", 1))
    if maximum_attempts <= 0:
        raise ValueError("GR01 bench session maximum attempts must be positive")
    sequences = () if route_blocked else range(1, config.minimum_round_trips + 1)
    for sequence in sequences:
        issued_at_s = wall_clock()
        command = TargetSelectionCommand(
            command_id=str(uuid4()),
            session_id=session_id,
            sequence=sequence,
            action=SelectionAction.SELECT,
            geometry=geometry,
            issued_at_s=issued_at_s,
            expires_at_s=issued_at_s + config.command_ttl_seconds,
            bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
            displayed_frame_id=f"gr01-bench-{sequence:06d}",
        )
        try:
            receipt = session.deliver(command)
        except SelectionDeliveryTimeout:
            timed_out_round_trips += 1
            transmission_attempts += maximum_attempts
            missing_ack_attempts += maximum_attempts
            continue
        attempts = int(receipt.attempts)
        transmission_attempts += attempts
        missing_ack_attempts += max(0, attempts - 1)
        if receipt.acknowledgement.accepted:
            accepted_round_trips += 1
            latencies_ms.append(float(receipt.elapsed_s) * 1_000.0)
        else:
            rejected_round_trips += 1

    packet_loss_rate = (
        missing_ack_attempts / transmission_attempts if transmission_attempts else 1.0
    )
    latency_p95_ms = _percentile(latencies_ms, 0.95)
    bidirectional_ip_verified = accepted_round_trips > 0 and (
        not config.hardware_mode
        or (remote_is_ipv4 and not remote_is_loopback and source_route_verified)
    )
    signed_round_trip = accepted_round_trips > 0
    reasons: list[str] = []
    if accepted_round_trips < config.minimum_round_trips:
        reasons.append("GR01 bench did not complete the required signed round trips")
    if packet_loss_rate > config.maximum_packet_loss_rate:
        reasons.append("GR01 packet-loss rate exceeds the configured limit")
    if latency_p95_ms is None or latency_p95_ms > config.maximum_ack_latency_p95_ms:
        reasons.append("GR01 ACK P95 latency exceeds the configured limit")
    if config.hardware_mode and remote_is_loopback:
        reasons.append("GR01 hardware mode cannot target a loopback address")
    if config.hardware_mode and not remote_is_ipv4:
        reasons.append("GR01 hardware mode requires a concrete IPv4 endpoint")
    if config.hardware_mode and not source_route_verified:
        reasons.append("GR01 local source address is outside the expected source CIDR")
    if not bidirectional_ip_verified:
        reasons.append("GR01 bidirectional IP acknowledgement is not verified")
    if not signed_round_trip:
        reasons.append("signed operator round trip is not verified")
    passed = not reasons
    timestamp = observed_at()
    if timestamp.tzinfo is None:
        raise ValueError("GR01 bench observation time must include a timezone")
    hardware_observed = config.hardware_mode and remote_is_ipv4 and not remote_is_loopback
    prefix = "gr01" if config.hardware_mode else "gr01_software_baseline"
    return {
        "schema_version": GR01_BENCH_SCHEMA_VERSION,
        "event": f"{prefix}_bench_{'passed' if passed else 'failed'}",
        "observed_at_utc": timestamp.astimezone(UTC).isoformat(),
        "hardware_observed": hardware_observed,
        "simulation_only": not config.hardware_mode,
        "passed": passed,
        "reasons": reasons,
        "hardware_model": "GR01" if config.hardware_mode else "software_loopback",
        "hardware_id": config.hardware_id.strip() if config.hardware_id else None,
        "remote_host": remote_host,
        "remote_port": remote_port,
        "remote_is_loopback": remote_is_loopback,
        "expected_source_cidr": expected_source_cidr,
        "local_source_address": local_source_address,
        "source_route_verified": source_route_verified,
        "requested_round_trips": config.minimum_round_trips,
        "round_trip_samples": accepted_round_trips,
        "rejected_round_trips": rejected_round_trips,
        "timed_out_round_trips": timed_out_round_trips,
        "transmission_attempts": transmission_attempts,
        "missing_ack_attempts": missing_ack_attempts,
        "packet_loss_rate": packet_loss_rate,
        "ack_latency_p50_ms": _percentile(latencies_ms, 0.50),
        "ack_latency_p95_ms": latency_p95_ms,
        "bidirectional_ip_verified": bidirectional_ip_verified,
        "signed_operator_round_trip": signed_round_trip,
        "application_hmac_verified": signed_round_trip,
        "mavlink2_signature_verified": signed_round_trip,
        "checks": {
            "required_round_trips": accepted_round_trips == config.minimum_round_trips,
            "packet_loss_limit": packet_loss_rate <= config.maximum_packet_loss_rate,
            "ack_latency_limit": (
                latency_p95_ms is not None and latency_p95_ms <= config.maximum_ack_latency_p95_ms
            ),
            "mode_boundary": not config.hardware_mode
            or (remote_is_ipv4 and not remote_is_loopback),
            "source_route": not config.hardware_mode or source_route_verified,
            "bidirectional_ack": bidirectional_ip_verified,
            "signed_operator_round_trip": signed_round_trip,
        },
        "thresholds": {
            "maximum_packet_loss_rate": config.maximum_packet_loss_rate,
            "maximum_ack_latency_p95_ms": config.maximum_ack_latency_p95_ms,
            "maximum_attempts": maximum_attempts,
        },
        "payload_release_requested": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "hardware_control_enabled": False,
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def resolve_local_source_address(host: str, port: int) -> str:
    """Return the concrete IPv4 source selected by the OS route table."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.connect((host, port))
        source = ipaddress.ip_address(channel.getsockname()[0])
    if source.version != 4 or source.is_unspecified:
        raise ValueError("GR01 route did not select a concrete IPv4 source address")
    return str(source)


def verify_gr01_link_bench_report(
    document: object, *, require_passed: bool = False
) -> dict[str, Any]:
    """Independently validate a GR01 bench report and recompute its gate result."""

    reasons: list[str] = []
    checks: dict[str, bool] = {}
    if not isinstance(document, dict):
        return _verification_result(False, checks, ["GR01 bench report must be an object"])

    expected_keys = {
        "schema_version",
        "event",
        "observed_at_utc",
        "hardware_observed",
        "simulation_only",
        "passed",
        "reasons",
        "hardware_model",
        "hardware_id",
        "remote_host",
        "remote_port",
        "remote_is_loopback",
        "expected_source_cidr",
        "local_source_address",
        "source_route_verified",
        "requested_round_trips",
        "round_trip_samples",
        "rejected_round_trips",
        "timed_out_round_trips",
        "transmission_attempts",
        "missing_ack_attempts",
        "packet_loss_rate",
        "ack_latency_p50_ms",
        "ack_latency_p95_ms",
        "bidirectional_ip_verified",
        "signed_operator_round_trip",
        "application_hmac_verified",
        "mavlink2_signature_verified",
        "checks",
        "thresholds",
        "payload_release_requested",
        "flight_control_enabled",
        "physical_release_enabled",
        "hardware_control_enabled",
    }
    checks["exact_fields"] = set(document) == expected_keys
    checks["schema_version"] = document.get("schema_version") == GR01_BENCH_SCHEMA_VERSION

    try:
        observed = datetime.fromisoformat(str(document.get("observed_at_utc")))
        checks["timestamp"] = observed.tzinfo is not None
    except ValueError:
        checks["timestamp"] = False

    simulation_only = document.get("simulation_only") is True
    hardware_mode = document.get("hardware_model") == "GR01" and not simulation_only
    checks["mode"] = hardware_mode or (
        document.get("hardware_model") == "software_loopback" and simulation_only
    )
    hardware_id = document.get("hardware_id")
    checks["hardware_id"] = (not hardware_mode and hardware_id is None) or (
        hardware_mode and isinstance(hardware_id, str) and bool(hardware_id.strip())
    )

    try:
        remote = ipaddress.ip_address(str(document.get("remote_host")))
        remote_valid = remote.version == 4 and str(remote) == document.get("remote_host")
    except ValueError:
        remote = None
        remote_valid = False
    checks["remote_endpoint"] = remote_valid and _plain_int_in_range(
        document.get("remote_port"), 1, 65535
    )
    remote_loopback = bool(remote is not None and remote.is_loopback)
    checks["remote_loopback_flag"] = document.get("remote_is_loopback") is remote_loopback

    source = None
    network = None
    try:
        if document.get("local_source_address") is not None:
            source = ipaddress.ip_address(str(document["local_source_address"]))
        if document.get("expected_source_cidr") is not None:
            network = ipaddress.ip_network(str(document["expected_source_cidr"]), strict=True)
    except ValueError:
        pass
    source_route = bool(
        source is not None
        and network is not None
        and source.version == network.version == 4
        and source in network
    )
    checks["source_route_flag"] = document.get("source_route_verified") is source_route
    checks["source_contract"] = (not hardware_mode and network is None) or (
        hardware_mode and network is not None and not network.is_loopback
    )

    count_names = (
        "requested_round_trips",
        "round_trip_samples",
        "rejected_round_trips",
        "timed_out_round_trips",
        "transmission_attempts",
        "missing_ack_attempts",
    )
    counts_valid = all(_plain_int_in_range(document.get(name), 0, None) for name in count_names)
    requested = document.get("requested_round_trips", -1)
    accepted = document.get("round_trip_samples", -1)
    rejected = document.get("rejected_round_trips", -1)
    timed_out = document.get("timed_out_round_trips", -1)
    attempts = document.get("transmission_attempts", -1)
    missing = document.get("missing_ack_attempts", -1)
    route_blocked = hardware_mode and not source_route
    expected_completed = 0 if route_blocked else requested
    checks["counts"] = bool(
        counts_valid
        and requested > 0
        and accepted + rejected + timed_out == expected_completed
        and attempts >= accepted + rejected
        and 0 <= missing <= attempts
    )
    checks["fail_closed_route"] = not route_blocked or (
        accepted == rejected == timed_out == attempts == missing == 0
    )

    try:
        expected_loss = missing / attempts if attempts else 1.0
        reported_loss = float(document.get("packet_loss_rate"))
        checks["packet_loss_math"] = math.isfinite(reported_loss) and math.isclose(
            reported_loss, expected_loss, rel_tol=0.0, abs_tol=1e-12
        )
        thresholds = document.get("thresholds")
        checks["thresholds"] = (
            isinstance(thresholds, dict)
            and set(thresholds)
            == {"maximum_packet_loss_rate", "maximum_ack_latency_p95_ms", "maximum_attempts"}
            and _finite_in_range(thresholds.get("maximum_packet_loss_rate"), 0.0, 1.0)
            and _finite_in_range(
                thresholds.get("maximum_ack_latency_p95_ms"), 0.0, None, open_min=True
            )
            and _plain_int_in_range(thresholds.get("maximum_attempts"), 1, None)
        )
    except (TypeError, ValueError, ZeroDivisionError):
        checks["packet_loss_math"] = False
        checks["thresholds"] = False
        reported_loss = math.inf
        thresholds = {}
    checks["attempt_accounting"] = bool(
        checks["counts"]
        and checks["thresholds"]
        and missing == attempts - accepted - rejected
        and attempts <= requested * int(thresholds["maximum_attempts"])
    )

    p50 = document.get("ack_latency_p50_ms")
    p95 = document.get("ack_latency_p95_ms")
    latency_shape = (p50 is None and p95 is None and accepted == 0) or (
        accepted > 0
        and _finite_in_range(p50, 0.0, None)
        and _finite_in_range(p95, 0.0, None)
        and float(p50) <= float(p95)
    )
    checks["latency_shape"] = latency_shape

    signed = accepted > 0
    bidirectional = signed and (
        not hardware_mode or (remote_valid and not remote_loopback and source_route)
    )
    checks["protocol_flags"] = all(
        (
            document.get("signed_operator_round_trip") is signed,
            document.get("application_hmac_verified") is signed,
            document.get("mavlink2_signature_verified") is signed,
            document.get("bidirectional_ip_verified") is bidirectional,
        )
    )
    checks["hardware_observed"] = document.get("hardware_observed") is (
        hardware_mode and remote_valid and not remote_loopback
    )
    checks["safe_controls"] = all(
        document.get(name) is False
        for name in (
            "payload_release_requested",
            "flight_control_enabled",
            "physical_release_enabled",
            "hardware_control_enabled",
        )
    )

    threshold_checks = document.get("checks")
    recomputed_threshold_checks = {
        "required_round_trips": accepted == requested,
        "packet_loss_limit": bool(checks.get("thresholds"))
        and reported_loss <= float(thresholds["maximum_packet_loss_rate"]),
        "ack_latency_limit": bool(checks.get("thresholds"))
        and p95 is not None
        and float(p95) <= float(thresholds["maximum_ack_latency_p95_ms"]),
        "mode_boundary": not hardware_mode or (remote_valid and not remote_loopback),
        "source_route": not hardware_mode or source_route,
        "bidirectional_ack": bidirectional,
        "signed_operator_round_trip": signed,
    }
    checks["embedded_checks"] = threshold_checks == recomputed_threshold_checks
    expected_reasons: list[str] = []
    if not recomputed_threshold_checks["required_round_trips"]:
        expected_reasons.append("GR01 bench did not complete the required signed round trips")
    if not recomputed_threshold_checks["packet_loss_limit"]:
        expected_reasons.append("GR01 packet-loss rate exceeds the configured limit")
    if not recomputed_threshold_checks["ack_latency_limit"]:
        expected_reasons.append("GR01 ACK P95 latency exceeds the configured limit")
    if hardware_mode and remote_loopback:
        expected_reasons.append("GR01 hardware mode cannot target a loopback address")
    if hardware_mode and not remote_valid:
        expected_reasons.append("GR01 hardware mode requires a concrete IPv4 endpoint")
    if hardware_mode and not source_route:
        expected_reasons.append("GR01 local source address is outside the expected source CIDR")
    if not bidirectional:
        expected_reasons.append("GR01 bidirectional IP acknowledgement is not verified")
    if not signed:
        expected_reasons.append("signed operator round trip is not verified")
    integrity_before_result = all(checks.values())
    recomputed_passed = integrity_before_result and all(recomputed_threshold_checks.values())
    prefix = "gr01" if hardware_mode else "gr01_software_baseline"
    expected_event = f"{prefix}_bench_{'passed' if recomputed_passed else 'failed'}"
    checks["result_fields"] = (
        document.get("passed") is recomputed_passed
        and document.get("event") == expected_event
        and document.get("reasons") == expected_reasons
    )
    recomputed_passed = all(checks.values()) and all(recomputed_threshold_checks.values())
    if require_passed and not recomputed_passed:
        reasons.append("GR01 bench report did not pass its measurement thresholds")
    reasons.extend(
        f"verification check failed: {name}" for name, value in checks.items() if not value
    )
    return _verification_result(recomputed_passed, checks, reasons, require_passed=require_passed)


def _plain_int_in_range(value: object, minimum: int, maximum: int | None) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _finite_in_range(
    value: object, minimum: float, maximum: float | None, *, open_min: bool = False
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return (
        math.isfinite(number)
        and (number > minimum if open_min else number >= minimum)
        and (maximum is None or number <= maximum)
    )


def _verification_result(
    recomputed_passed: bool,
    checks: dict[str, bool],
    reasons: list[str],
    *,
    require_passed: bool = False,
) -> dict[str, Any]:
    gate_passed = recomputed_passed if require_passed else all(checks.values())
    return {
        "schema_version": GR01_BENCH_SCHEMA_VERSION,
        "event": "gr01_link_bench_verified" if gate_passed else "gr01_link_bench_rejected",
        "gate_passed": gate_passed,
        "recomputed_passed": recomputed_passed,
        "checks": checks,
        "reasons": reasons,
    }


def verify_main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Verify one GR01 link bench report")
    parser.add_argument("report", type=Path)
    parser.add_argument("--require-passed", action="store_true")
    args = parser.parse_args(argv)
    try:
        document = strict_json_loads(args.report.read_bytes(), name="GR01 link bench report")
        result = verify_gr01_link_bench_report(document, require_passed=args.require_passed)
    except (OSError, ValueError) as exc:
        result = _verification_result(False, {}, [f"unable to load report: {exc}"])
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["gate_passed"] else 1


__all__ = [
    "GR01_BENCH_SCHEMA_VERSION",
    "Gr01BenchConfig",
    "resolve_local_source_address",
    "run_gr01_link_bench",
    "verify_gr01_link_bench_report",
    "verify_main",
]

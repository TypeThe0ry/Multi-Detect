"""Bounded authenticated alert-link round-trip bench.

The production alert path already persists an alert before transmission and
requires a correlated acknowledgement before marking it delivered.  This
module exercises that same transport contract repeatedly and emits a small,
hashable report that can be attached to a field bring-up session.  It never
enables flight control or a payload output.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .alert_link_topology import (
    AlertLinkTopology,
    load_alert_link_topology,
    parse_alert_link_topology,
)
from .alerts import (
    AcknowledgedAlertTransport,
    AlertAcknowledgementError,
    RetryingAcknowledgedAlertPublisher,
    UdpAcknowledgedAlertTransport,
)
from .autonomy.serialization import canonical_sha256, strict_json_loads
from .compat import UTC
from .domain import BoundingBox, FireAlert

ALERT_LINK_BENCH_SCHEMA_VERSION = 5
SUPPORTED_ALERT_LINK_BENCH_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5})
DEFAULT_MINIMUM_ROUND_TRIPS = 20
DEFAULT_MAXIMUM_ATTEMPTS = 3
DEFAULT_ACK_TIMEOUT_SECONDS = 1.0
DEFAULT_MAXIMUM_PACKET_LOSS_RATE = 0.01
DEFAULT_MAXIMUM_ACK_LATENCY_P95_MS = 500.0


def _finite_float(value: object) -> float | None:
    """Return a finite float for JSON/config numeric values, otherwise ``None``."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


@dataclass(frozen=True, slots=True)
class AlertLinkBenchConfig:
    """Acceptance thresholds for the acknowledged alert data-link bench."""

    minimum_round_trips: int = DEFAULT_MINIMUM_ROUND_TRIPS
    maximum_attempts: int = DEFAULT_MAXIMUM_ATTEMPTS
    initial_backoff_seconds: float = 0.1
    maximum_backoff_seconds: float = 5.0
    maximum_packet_loss_rate: float = DEFAULT_MAXIMUM_PACKET_LOSS_RATE
    maximum_ack_latency_p95_ms: float = DEFAULT_MAXIMUM_ACK_LATENCY_P95_MS
    hardware_mode: bool = False
    hardware_id: str | None = None
    expected_source_cidr: str | None = None
    topology: AlertLinkTopology | None = None

    def __post_init__(self) -> None:
        if type(self.hardware_mode) is not bool:
            raise ValueError("alert-link hardware mode must be a boolean")
        if (
            isinstance(self.minimum_round_trips, bool)
            or not isinstance(self.minimum_round_trips, int)
            or self.minimum_round_trips <= 0
        ):
            raise ValueError("alert-link bench minimum round trips must be a positive integer")
        if (
            isinstance(self.maximum_attempts, bool)
            or not isinstance(self.maximum_attempts, int)
            or self.maximum_attempts <= 0
        ):
            raise ValueError("alert-link bench maximum attempts must be a positive integer")
        for name, value in (
            ("initial backoff", self.initial_backoff_seconds),
            ("maximum backoff", self.maximum_backoff_seconds),
        ):
            finite_value = _finite_float(value)
            if finite_value is None or finite_value < 0:
                raise ValueError(f"alert-link bench {name} must be finite and non-negative")
        if self.maximum_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError(
                "alert-link bench maximum backoff must be at least the initial backoff"
            )
        maximum_packet_loss_rate = _finite_float(self.maximum_packet_loss_rate)
        if maximum_packet_loss_rate is None or not 0 <= maximum_packet_loss_rate <= 1:
            raise ValueError("alert-link bench packet-loss limit must be in [0, 1]")
        maximum_ack_latency_p95_ms = _finite_float(self.maximum_ack_latency_p95_ms)
        if maximum_ack_latency_p95_ms is None or maximum_ack_latency_p95_ms <= 0:
            raise ValueError("alert-link bench ACK P95 limit must be finite and positive")
        if self.hardware_mode and (
            not isinstance(self.hardware_id, str) or not self.hardware_id.strip()
        ):
            raise ValueError("alert-link hardware mode requires a non-empty hardware ID")
        if not self.hardware_mode and self.hardware_id is not None:
            raise ValueError("alert-link hardware ID requires hardware mode")
        if self.hardware_mode and (
            not isinstance(self.expected_source_cidr, str) or not self.expected_source_cidr.strip()
        ):
            raise ValueError("alert-link hardware mode requires an expected source CIDR")
        if not self.hardware_mode and self.expected_source_cidr is not None:
            raise ValueError("alert-link expected source CIDR requires hardware mode")
        if self.expected_source_cidr is not None:
            try:
                source_network = ipaddress.ip_network(
                    self.expected_source_cidr.strip(), strict=False
                )
            except ValueError as exc:
                raise ValueError("alert-link expected source CIDR is invalid") from exc
            if source_network.is_loopback:
                raise ValueError("alert-link hardware source CIDR cannot be loopback")
        if self.topology is not None and not self.hardware_mode:
            raise ValueError("alert-link topology requires hardware mode")
        if self.topology is not None:
            if self.hardware_id != self.topology.hardware_id:
                raise ValueError("alert-link hardware ID conflicts with topology")
            if _canonical_network(self.expected_source_cidr) != self.topology.expected_source_cidr:
                raise ValueError("alert-link source CIDR conflicts with topology")


def run_alert_link_bench(
    transport: AcknowledgedAlertTransport,
    config: AlertLinkBenchConfig,
    *,
    wall_clock: Callable[[], float] = time.time,
    monotonic_clock: Callable[[], float] = time.monotonic,
    observed_at: Callable[[], datetime] = lambda: datetime.now(UTC),
    source_address_resolver: Callable[[str, int], str] | None = None,
) -> dict[str, Any]:
    """Run bounded acknowledged alert probes through ``transport``.

    ``transport`` is the same port used by the live patrol.  A successful
    sample means the receiver returned a matching, validated receipt.  Retry
    attempts that do not receive an acknowledgement are counted in the packet
    loss estimate; a final failed sample never satisfies the required sample
    count.  The optional ``hardware_mode`` flag is an explicit field-session
    assertion and still rejects loopback endpoints. Hardware reports also bind
    the source address selected by the operating system to an explicit CIDR;
    this prevents a responsive peer on the wrong interface from being promoted
    to selected-link evidence.
    """

    session_id = f"alert-link-bench-{uuid4()}"
    remote_host, remote_port, remote_is_loopback = _remote_details(transport)
    expected_source_cidr = _canonical_network(config.expected_source_cidr)
    topology_document = config.topology.to_mapping() if config.topology is not None else None
    topology_sha256 = config.topology.sha256 if config.topology is not None else None
    topology_binding_verified = not config.hardware_mode or (
        config.topology is not None
        and remote_host == config.topology.remote_host
        and remote_port == config.topology.remote_port
        and expected_source_cidr == config.topology.expected_source_cidr
        and getattr(transport, "sender_id", None) == config.topology.sender_id
        and getattr(transport, "receiver_id", None) == config.topology.receiver_id
    )
    local_source_address: str | None = None
    source_route_verified = False
    transport_source_address = (
        transport.source_host if isinstance(transport, UdpAcknowledgedAlertTransport) else None
    )
    transport_source_address_bound = False
    if (
        config.hardware_mode
        and remote_host is not None
        and remote_port is not None
        and not remote_is_loopback
    ):
        resolver = source_address_resolver or _resolve_local_source_address
        try:
            local_source_address = resolver(remote_host, remote_port)
            source_route_verified = _address_in_network(local_source_address, expected_source_cidr)
        except (OSError, TypeError, ValueError):
            local_source_address = None
    if config.hardware_mode:
        transport_source_address_bound = (
            source_route_verified
            and transport_source_address is not None
            and transport_source_address == local_source_address
        )
    route_blocked = config.hardware_mode and (
        not source_route_verified or not transport_source_address_bound
    )
    publisher = RetryingAcknowledgedAlertPublisher(
        transport,
        maximum_attempts=config.maximum_attempts,
        initial_backoff_seconds=config.initial_backoff_seconds,
        maximum_backoff_seconds=config.maximum_backoff_seconds,
    )
    accepted_round_trips = 0
    failed_round_trips = 0
    transmission_attempts = 0
    missing_ack_attempts = 0
    latencies_ms: list[float] = []
    observed_transport_source_addresses: list[str] = []
    errors: list[str] = []
    attempt_counts: list[int] = []
    successful_samples: list[bool] = []

    sequences: Sequence[int] = () if route_blocked else range(1, config.minimum_round_trips + 1)
    for sequence in sequences:
        observed_at_s = _finite_float(wall_clock())
        if observed_at_s is None:
            raise ValueError("alert-link bench wall clock must return a finite number")
        alert = FireAlert(
            alert_id=f"{session_id}-{sequence:04d}",
            mission_id=session_id,
            target_id=f"{session_id}-target",
            target_revision=1,
            frame_id=f"{session_id}-frame-{sequence:04d}",
            label="flame",
            confidence=0.99,
            bbox=BoundingBox(0.40, 0.35, 0.60, 0.65),
            observed_at_s=max(0.0, observed_at_s),
        )
        started = _finite_float(monotonic_clock())
        if started is None:
            raise ValueError("alert-link bench monotonic clock must return a finite number")
        try:
            publisher.publish(alert)
        except (AlertAcknowledgementError, OSError, RuntimeError, TypeError, ValueError) as exc:
            failed_round_trips += 1
            errors.append(type(exc).__name__)
            succeeded = False
        else:
            finished = _finite_float(monotonic_clock())
            elapsed_ms = (finished - started) * 1_000.0 if finished is not None else -1.0
            if finished is None or not math.isfinite(elapsed_ms) or elapsed_ms < 0:
                raise ValueError("alert-link bench monotonic clock moved backwards")
            accepted_round_trips += 1
            latencies_ms.append(elapsed_ms)
            receipt = publisher.last_receipt
            if receipt is not None and receipt.source_address is not None:
                observed_transport_source_addresses.append(receipt.source_address)
            succeeded = True
        attempts = publisher.last_attempt_count
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts <= 0
            or attempts > config.maximum_attempts
        ):
            raise RuntimeError("alert-link publisher returned an invalid attempt count")
        attempt_counts.append(attempts)
        successful_samples.append(succeeded)
        transmission_attempts += attempts
        missing_ack_attempts += attempts - 1 if succeeded else attempts

    expected_sample_count = 0 if route_blocked else config.minimum_round_trips
    if (
        len(attempt_counts) != expected_sample_count
        or len(successful_samples) != expected_sample_count
    ):
        raise RuntimeError("alert-link bench sample accounting is incomplete")
    transport_protocol = (
        "authenticated_udp"
        if isinstance(transport, UdpAcknowledgedAlertTransport)
        else type(transport).__name__
    )
    packet_loss_rate = (
        missing_ack_attempts / transmission_attempts if transmission_attempts else 1.0
    )
    latency_p50_ms = _percentile(latencies_ms, 0.50)
    latency_p95_ms = _percentile(latencies_ms, 0.95)
    correlated_ack_verified = accepted_round_trips > 0
    authenticated_ack_verified = (
        correlated_ack_verified and transport_protocol == "authenticated_udp"
    )
    transport_source_observations_complete = not config.hardware_mode or (
        accepted_round_trips == config.minimum_round_trips
        and len(observed_transport_source_addresses) == accepted_round_trips
        and all(
            address == transport_source_address for address in observed_transport_source_addresses
        )
    )
    mode_boundary_verified = not config.hardware_mode or (
        not remote_is_loopback
        and isinstance(config.hardware_id, str)
        and bool(config.hardware_id.strip())
    )
    checks = {
        "required_round_trips": accepted_round_trips == config.minimum_round_trips,
        "packet_loss_limit": packet_loss_rate <= config.maximum_packet_loss_rate,
        "ack_latency_limit": (
            latency_p95_ms is not None and latency_p95_ms <= config.maximum_ack_latency_p95_ms
        ),
        "mode_boundary": mode_boundary_verified,
        "source_route": source_route_verified or not config.hardware_mode,
        "transport_source_binding": transport_source_address_bound or not config.hardware_mode,
        "transport_source_observations": transport_source_observations_complete,
        "topology_binding": topology_binding_verified,
        "correlated_ack": correlated_ack_verified,
        "authenticated_transport": authenticated_ack_verified or not config.hardware_mode,
    }
    reasons: list[str] = []
    if not checks["required_round_trips"]:
        reasons.append("alert-link bench did not complete the required acknowledged round trips")
    if not checks["packet_loss_limit"]:
        reasons.append("alert-link missing-ack rate exceeds the configured limit")
    if not checks["ack_latency_limit"]:
        reasons.append("alert-link ACK P95 latency exceeds the configured limit")
    if config.hardware_mode and not mode_boundary_verified:
        reasons.append("alert-link hardware mode cannot target a loopback or unresolved address")
    if config.hardware_mode and not checks["source_route"]:
        reasons.append("alert-link local source address is outside the expected source CIDR")
    if config.hardware_mode and not checks["transport_source_binding"]:
        reasons.append("alert-link transport socket is not bound to the selected source address")
    if config.hardware_mode and not checks["transport_source_observations"]:
        reasons.append("alert-link ACK samples do not prove the bound sender source address")
    if config.hardware_mode and not checks["topology_binding"]:
        reasons.append("alert-link endpoint or source route is not bound to a verified topology")
    if not checks["correlated_ack"]:
        reasons.append("correlated alert acknowledgement is not verified")
    if config.hardware_mode and not checks["authenticated_transport"]:
        reasons.append("hardware alert-link evidence requires the authenticated UDP transport")
    passed = not reasons
    hardware_observed = config.hardware_mode and not remote_is_loopback
    return {
        "schema_version": ALERT_LINK_BENCH_SCHEMA_VERSION,
        "event": ("alert_link_bench_passed" if passed else "alert_link_bench_failed")
        if config.hardware_mode
        else (
            "alert_link_software_baseline_bench_passed"
            if passed
            else "alert_link_software_baseline_bench_failed"
        ),
        "observed_at_utc": _observed_at_iso(observed_at),
        "hardware_observed": hardware_observed,
        "simulation_only": not config.hardware_mode,
        "passed": passed,
        "reasons": reasons,
        "checks": checks,
        "transport_protocol": transport_protocol,
        "remote_host": remote_host,
        "remote_port": remote_port,
        "remote_is_loopback": remote_is_loopback,
        "expected_source_cidr": expected_source_cidr,
        "local_source_address": local_source_address,
        "source_route_verified": source_route_verified,
        "transport_source_address": transport_source_address,
        "transport_source_address_bound": transport_source_address_bound,
        "observed_transport_source_addresses": observed_transport_source_addresses,
        "transport_source_observation_count": len(observed_transport_source_addresses),
        "transport_source_observations_complete": transport_source_observations_complete,
        "hardware_id": config.hardware_id.strip() if config.hardware_id else None,
        "topology": topology_document,
        "topology_sha256": topology_sha256,
        "topology_binding_verified": topology_binding_verified,
        "requested_round_trips": config.minimum_round_trips,
        "round_trip_samples": accepted_round_trips,
        "failed_round_trips": failed_round_trips,
        "transmission_attempts": transmission_attempts,
        "missing_ack_attempts": missing_ack_attempts,
        "packet_loss_rate": packet_loss_rate,
        "ack_latency_p50_ms": latency_p50_ms,
        "ack_latency_p95_ms": latency_p95_ms,
        "correlated_ack_verified": correlated_ack_verified,
        "authenticated_ack_verified": authenticated_ack_verified,
        "failure_types": dict(sorted((name, errors.count(name)) for name in set(errors))),
        "thresholds": {
            "maximum_packet_loss_rate": config.maximum_packet_loss_rate,
            "maximum_ack_latency_p95_ms": config.maximum_ack_latency_p95_ms,
            "maximum_attempts": config.maximum_attempts,
        },
        "alert_hardware_control_enabled": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "payload_release_requested": False,
        "production_approved": False,
        "physical_release_approved": False,
    }


def verify_alert_link_bench_report(
    document: object,
    *,
    require_passed: bool = False,
) -> dict[str, Any]:
    """Recompute the report invariants without opening a socket.

    The verifier is intentionally independent from the transport.  It catches
    copied ``passed`` bits, altered packet-loss/latency values, mode-boundary
    drift and any attempt to turn a software baseline into hardware evidence.
    """

    reasons: list[str] = []
    if not isinstance(document, dict):
        return {
            "schema_version": ALERT_LINK_BENCH_SCHEMA_VERSION,
            "event": "alert_link_bench_verified",
            "gate_passed": False,
            "recomputed_passed": False,
            "checks": {},
            "reasons": ["alert-link bench report must be an object"],
        }

    schema_version = document.get("schema_version")
    if schema_version not in SUPPORTED_ALERT_LINK_BENCH_SCHEMA_VERSIONS:
        reasons.append("schema_version must be 1, 2, 3, 4 or 5")
    is_route_bound = schema_version in {2, 3, 4, 5}
    is_transport_bound = schema_version in {3, 4, 5}
    has_transport_observations = schema_version in {4, 5}
    has_topology_binding = schema_version == 5
    event = document.get("event")
    if isinstance(event, str):
        hardware_event = event in {
            "alert_link_bench_passed",
            "alert_link_bench_failed",
        }
        software_event = event in {
            "alert_link_software_baseline_bench_passed",
            "alert_link_software_baseline_bench_failed",
        }
    else:
        hardware_event = False
        software_event = False
    if not (hardware_event or software_event):
        reasons.append("event is not a supported alert-link bench event")
    passed = document.get("passed")
    if type(passed) is not bool:
        reasons.append("passed must be a boolean")
        passed = False
    reasons_field = document.get("reasons")
    if not isinstance(reasons_field, list) or not all(
        isinstance(item, str) for item in reasons_field
    ):
        reasons.append("reasons must be a list of strings")
        reasons_field = []
    observed_at = document.get("observed_at_utc")
    if not isinstance(observed_at, str) or not observed_at.strip():
        reasons.append("observed_at_utc must be a non-empty string")
    else:
        try:
            parsed_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            parsed_time = None
        if parsed_time is None or parsed_time.tzinfo is None:
            reasons.append("observed_at_utc must be an ISO-8601 timestamp with timezone")

    def _bool_field(name: str) -> bool:
        value = document.get(name)
        if type(value) is not bool:
            reasons.append(f"{name} must be a boolean")
            return False
        return value

    hardware_observed = _bool_field("hardware_observed")
    simulation_only = _bool_field("simulation_only")
    remote_is_loopback = _bool_field("remote_is_loopback")
    correlated_ack = _bool_field("correlated_ack_verified")
    authenticated_ack = _bool_field("authenticated_ack_verified")
    for name in (
        "alert_hardware_control_enabled",
        "flight_control_enabled",
        "physical_release_enabled",
        "payload_release_requested",
        "production_approved",
        "physical_release_approved",
    ):
        if _bool_field(name):
            reasons.append(f"{name} must remain false")

    protocol = document.get("transport_protocol")
    if not isinstance(protocol, str) or not protocol.strip():
        reasons.append("transport_protocol must be a non-empty string")
        protocol = ""
    remote_host = document.get("remote_host")
    if remote_host is not None and (not isinstance(remote_host, str) or not remote_host.strip()):
        reasons.append("remote_host must be a non-empty string or null")
    endpoint_is_concrete_ip = False
    endpoint_loopback = True
    if isinstance(remote_host, str) and remote_host.strip():
        try:
            endpoint_loopback = ipaddress.ip_address(remote_host.strip()).is_loopback
        except ValueError:
            # Hostnames are intentionally unresolved here.  They cannot be
            # promoted to hardware evidence by copying a boolean field.
            endpoint_loopback = True
        else:
            endpoint_is_concrete_ip = True
    remote_port = document.get("remote_port")
    if remote_port is not None and (
        isinstance(remote_port, bool)
        or not isinstance(remote_port, int)
        or not 1 <= remote_port <= 65_535
    ):
        reasons.append("remote_port must be an integer in [1, 65535] or null")
    endpoint_port_valid = (
        isinstance(remote_port, int)
        and not isinstance(remote_port, bool)
        and 1 <= remote_port <= 65_535
    )
    if remote_is_loopback is not endpoint_loopback:
        reasons.append("remote_is_loopback does not match the reported endpoint")
    expected_source_cidr: str | None = None
    local_source_address: str | None = None
    source_route_verified = False
    transport_source_address: str | None = None
    transport_source_address_bound = False
    if is_route_bound:
        raw_source_cidr = document.get("expected_source_cidr")
        raw_source_address = document.get("local_source_address")
        if raw_source_cidr is not None and (
            not isinstance(raw_source_cidr, str) or not raw_source_cidr.strip()
        ):
            reasons.append("expected_source_cidr must be a non-empty string or null")
        elif isinstance(raw_source_cidr, str):
            try:
                source_network = ipaddress.ip_network(raw_source_cidr.strip(), strict=False)
            except ValueError:
                reasons.append("expected_source_cidr must be a valid IP network")
            else:
                expected_source_cidr = str(source_network)
                if raw_source_cidr.strip() != expected_source_cidr:
                    reasons.append("expected_source_cidr must use canonical network notation")
                if source_network.is_loopback:
                    reasons.append("expected_source_cidr cannot be loopback in a hardware report")
        if raw_source_address is not None and (
            not isinstance(raw_source_address, str) or not raw_source_address.strip()
        ):
            reasons.append("local_source_address must be a non-empty string or null")
        elif isinstance(raw_source_address, str):
            try:
                local_source_address = str(ipaddress.ip_address(raw_source_address.strip()))
            except ValueError:
                reasons.append("local_source_address must be a concrete IP address")
            else:
                if raw_source_address.strip() != local_source_address:
                    reasons.append("local_source_address must use canonical IP notation")
        declared_source_route = document.get("source_route_verified")
        if type(declared_source_route) is not bool:
            reasons.append("source_route_verified must be a boolean")
        source_route_verified = _address_in_network(local_source_address, expected_source_cidr)
        if (
            type(declared_source_route) is bool
            and declared_source_route is not source_route_verified
        ):
            reasons.append("source_route_verified does not match the source address and CIDR")
        if software_event and any(
            value is not None for value in (raw_source_cidr, raw_source_address)
        ):
            reasons.append("software baseline cannot claim a hardware source route")
        if software_event and declared_source_route is not False:
            reasons.append("software baseline source_route_verified must be false")
    else:
        for field in (
            "expected_source_cidr",
            "local_source_address",
            "source_route_verified",
        ):
            if field in document:
                reasons.append(f"schema v1 report cannot contain {field}")
    if is_transport_bound:
        raw_transport_source = document.get("transport_source_address")
        if raw_transport_source is not None and (
            not isinstance(raw_transport_source, str) or not raw_transport_source.strip()
        ):
            reasons.append("transport_source_address must be a non-empty string or null")
        elif isinstance(raw_transport_source, str):
            try:
                transport_source_address = str(ipaddress.ip_address(raw_transport_source.strip()))
            except ValueError:
                reasons.append("transport_source_address must be a concrete IP address")
            else:
                if raw_transport_source.strip() != transport_source_address:
                    reasons.append("transport_source_address must use canonical IP notation")
        declared_transport_binding = document.get("transport_source_address_bound")
        if type(declared_transport_binding) is not bool:
            reasons.append("transport_source_address_bound must be a boolean")
        transport_source_address_bound = (
            hardware_event
            and source_route_verified
            and transport_source_address is not None
            and transport_source_address == local_source_address
        )
        if (
            type(declared_transport_binding) is bool
            and declared_transport_binding is not transport_source_address_bound
        ):
            reasons.append(
                "transport_source_address_bound does not match the selected source address"
            )
        if software_event and raw_transport_source is not None:
            reasons.append("software baseline cannot bind a transport source address")
        if software_event and declared_transport_binding is not False:
            reasons.append("software baseline transport_source_address_bound must be false")
    else:
        for field in ("transport_source_address", "transport_source_address_bound"):
            if field in document:
                reasons.append(f"schema v1/v2 report cannot contain {field}")
    expected_hardware_observed = (
        hardware_event and endpoint_is_concrete_ip and endpoint_port_valid and not endpoint_loopback
    )
    if hardware_observed is not expected_hardware_observed:
        reasons.append("hardware_observed does not match the endpoint and event domain")

    def _nonnegative_int(name: str, *, minimum: int = 0) -> int | None:
        value = document.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            reasons.append(f"{name} must be an integer >= {minimum}")
            return None
        return value

    requested = _nonnegative_int("requested_round_trips", minimum=1)
    samples = _nonnegative_int("round_trip_samples")
    failed = _nonnegative_int("failed_round_trips")
    transmissions = _nonnegative_int("transmission_attempts")
    missing = _nonnegative_int("missing_ack_attempts")
    observed_transport_source_addresses: list[str] = []
    transport_source_observation_count: int | None = None
    declared_transport_source_observations_complete = False
    if has_transport_observations:
        raw_observed_sources = document.get("observed_transport_source_addresses")
        if not isinstance(raw_observed_sources, list):
            reasons.append("observed_transport_source_addresses must be a list")
        else:
            for raw_address in raw_observed_sources:
                if not isinstance(raw_address, str) or not raw_address.strip():
                    reasons.append(
                        "observed_transport_source_addresses must contain non-empty strings"
                    )
                    continue
                try:
                    canonical_address = str(ipaddress.ip_address(raw_address.strip()))
                except ValueError:
                    reasons.append(
                        "observed_transport_source_addresses must contain concrete IP addresses"
                    )
                    continue
                if (
                    canonical_address != raw_address
                    or ipaddress.ip_address(canonical_address).version != 4
                ):
                    reasons.append(
                        "observed_transport_source_addresses must contain canonical IPv4 addresses"
                    )
                    continue
                observed_transport_source_addresses.append(canonical_address)
        transport_source_observation_count = _nonnegative_int("transport_source_observation_count")
        raw_observations_complete = document.get("transport_source_observations_complete")
        if type(raw_observations_complete) is not bool:
            reasons.append("transport_source_observations_complete must be a boolean")
        else:
            declared_transport_source_observations_complete = raw_observations_complete
    else:
        for field in (
            "observed_transport_source_addresses",
            "transport_source_observation_count",
            "transport_source_observations_complete",
        ):
            if field in document:
                reasons.append(f"schema v1/v2/v3 report cannot contain {field}")
    hardware_id = document.get("hardware_id")
    if hardware_event:
        if not isinstance(hardware_id, str) or not hardware_id.strip():
            reasons.append("hardware event requires a non-empty hardware_id")
    elif hardware_id is not None:
        reasons.append("software baseline hardware_id must be null")

    topology_binding = not hardware_event
    if has_topology_binding:
        topology_document = document.get("topology")
        topology_hash = document.get("topology_sha256")
        declared_topology_binding = document.get("topology_binding_verified")
        if software_event:
            topology_binding = topology_document is None and topology_hash is None
        elif isinstance(topology_document, dict):
            try:
                topology = parse_alert_link_topology(
                    topology_document,
                    base_dir=Path.cwd(),
                    verify_evidence_files=False,
                )
            except ValueError as exc:
                reasons.append(f"topology snapshot is invalid: {exc}")
            else:
                topology_binding = (
                    isinstance(topology_hash, str)
                    and topology_hash == canonical_sha256(topology_document)
                    and topology.remote_host == remote_host
                    and topology.remote_port == remote_port
                    and topology.expected_source_cidr == expected_source_cidr
                    and topology.hardware_id == hardware_id
                )
        if type(declared_topology_binding) is not bool:
            reasons.append("topology_binding_verified must be a boolean")
        elif declared_topology_binding is not topology_binding:
            reasons.append("topology_binding_verified does not match the topology snapshot")
        if not topology_binding:
            reasons.append("topology snapshot/hash does not bind the hardware endpoint")
    else:
        for field in ("topology", "topology_sha256", "topology_binding_verified"):
            if field in document:
                reasons.append(f"schema v1/v2/v3/v4 report cannot contain {field}")

    packet_loss = document.get("packet_loss_rate")
    packet_loss_value = _finite_float(packet_loss)
    if packet_loss_value is None or not 0 <= packet_loss_value <= 1:
        reasons.append("packet_loss_rate must be a finite number in [0, 1]")
        packet_loss = None
    else:
        packet_loss = packet_loss_value
    p50 = document.get("ack_latency_p50_ms")
    p95 = document.get("ack_latency_p95_ms")
    p50_value = _finite_float(p50) if p50 is not None else None
    p95_value = _finite_float(p95) if p95 is not None else None
    for name, value, finite_value in (
        ("ack_latency_p50_ms", p50, p50_value),
        ("ack_latency_p95_ms", p95, p95_value),
    ):
        if value is not None and (finite_value is None or finite_value < 0):
            reasons.append(f"{name} must be a finite non-negative number or null")
    if p50_value is not None and p95_value is not None and p50_value > p95_value:
        reasons.append("ack_latency_p50_ms cannot exceed ack_latency_p95_ms")

    thresholds = document.get("thresholds")
    if not isinstance(thresholds, dict):
        reasons.append("thresholds must be an object")
        thresholds = {}
    maximum_loss = thresholds.get("maximum_packet_loss_rate")
    maximum_p95 = thresholds.get("maximum_ack_latency_p95_ms")
    maximum_attempts = thresholds.get("maximum_attempts")
    maximum_loss_value = _finite_float(maximum_loss)
    if maximum_loss_value is None or not 0 <= maximum_loss_value <= 1:
        reasons.append("thresholds.maximum_packet_loss_rate is invalid")
        maximum_loss = 1.0
    else:
        maximum_loss = maximum_loss_value
    maximum_p95_value = _finite_float(maximum_p95)
    if maximum_p95_value is None or maximum_p95_value <= 0:
        reasons.append("thresholds.maximum_ack_latency_p95_ms is invalid")
        maximum_p95 = float("inf")
    else:
        maximum_p95 = maximum_p95_value
    if (
        isinstance(maximum_attempts, bool)
        or not isinstance(maximum_attempts, int)
        or maximum_attempts <= 0
    ):
        reasons.append("thresholds.maximum_attempts is invalid")
        maximum_attempts = 1

    route_preflight_blocked = (
        is_route_bound
        and hardware_event
        and (
            not source_route_verified or (is_transport_bound and not transport_source_address_bound)
        )
    )
    if requested is not None and samples is not None and failed is not None:
        if route_preflight_blocked and (samples != 0 or failed != 0):
            reasons.append("source-route-blocked report cannot contain round-trip outcomes")
        elif not route_preflight_blocked and samples + failed != requested:
            reasons.append("round_trip_samples plus failed_round_trips must equal requested")
    if route_preflight_blocked and any(
        value not in {None, 0} for value in (transmissions, missing)
    ):
        reasons.append("source-route-blocked report cannot contain transmission attempts")
    if samples is not None and transmissions is not None and transmissions < samples:
        reasons.append("transmission_attempts cannot be below successful samples")
    if failed is not None and transmissions is not None and transmissions < failed:
        reasons.append("transmission_attempts cannot be below failed samples")
    if missing is not None and transmissions is not None and missing > transmissions:
        reasons.append("missing_ack_attempts cannot exceed transmission_attempts")
    if (
        packet_loss is not None
        and missing is not None
        and transmissions is not None
        and transmissions > 0
        and not math.isclose(
            float(packet_loss), missing / transmissions, rel_tol=1e-12, abs_tol=1e-12
        )
    ):
        reasons.append("packet_loss_rate does not match missing/transmission attempts")
    if samples is not None and correlated_ack is not (samples > 0):
        reasons.append("correlated_ack_verified does not match round_trip_samples")
    if authenticated_ack and protocol != "authenticated_udp":
        reasons.append("authenticated_ack_verified requires authenticated_udp transport")
    if hardware_event:
        if simulation_only:
            reasons.append("hardware event cannot set simulation_only=true")
        if not hardware_observed:
            reasons.append("hardware event requires hardware_observed=true")
        if not endpoint_is_concrete_ip or not endpoint_port_valid:
            reasons.append("hardware event requires a concrete IP endpoint and valid port")
        if endpoint_loopback:
            reasons.append("hardware event cannot use a loopback or unresolved endpoint")
    if software_event:
        if not simulation_only:
            reasons.append("software baseline must set simulation_only=true")
        if hardware_observed:
            reasons.append("software baseline cannot set hardware_observed=true")
    if event is not None and isinstance(event, str):
        expected_suffix = "passed" if passed else "failed"
        if not event.endswith(expected_suffix):
            reasons.append("event suffix does not match passed")

    failure_types = document.get("failure_types")
    failure_type_total = 0
    if not isinstance(failure_types, dict):
        reasons.append("failure_types must be an object")
    else:
        for name, count in failure_types.items():
            if (
                not isinstance(name, str)
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count <= 0
            ):
                reasons.append("failure_types must map names to positive integers")
                break
            failure_type_total += count
        if failed is not None and failure_type_total != failed:
            reasons.append("failure_types counts must equal failed_round_trips")

    mode_boundary = (not hardware_event) or (
        hardware_observed
        and endpoint_is_concrete_ip
        and endpoint_port_valid
        and not endpoint_loopback
        and isinstance(hardware_id, str)
    )
    source_route = (not hardware_event) or (not is_route_bound) or source_route_verified
    transport_source_binding = (
        (not hardware_event) or (not is_transport_bound) or transport_source_address_bound
    )
    transport_source_observations = (
        (not hardware_event)
        or (not has_transport_observations)
        or (
            samples is not None
            and samples == requested
            and transport_source_observation_count == samples
            and len(observed_transport_source_addresses) == samples
            and all(
                address == transport_source_address
                for address in observed_transport_source_addresses
            )
        )
    )
    if has_transport_observations:
        if transport_source_observation_count != len(observed_transport_source_addresses):
            reasons.append(
                "transport_source_observation_count does not match observed source addresses"
            )
        if declared_transport_source_observations_complete is not transport_source_observations:
            reasons.append(
                "transport_source_observations_complete does not match ACK sample observations"
            )
    required_round_trips = requested is not None and samples == requested
    packet_loss_limit = packet_loss is not None and float(packet_loss) <= float(maximum_loss)
    ack_latency_limit = p95_value is not None and p95_value <= float(maximum_p95)
    checks = {
        "required_round_trips": required_round_trips,
        "packet_loss_limit": packet_loss_limit,
        "ack_latency_limit": ack_latency_limit,
        "mode_boundary": mode_boundary,
        **({"source_route": source_route} if is_route_bound else {}),
        **({"transport_source_binding": transport_source_binding} if is_transport_bound else {}),
        **(
            {"transport_source_observations": transport_source_observations}
            if has_transport_observations
            else {}
        ),
        **({"topology_binding": topology_binding} if has_topology_binding else {}),
        "correlated_ack": correlated_ack,
        "authenticated_transport": authenticated_ack or not hardware_event,
    }
    declared_checks = document.get("checks")
    if not isinstance(declared_checks, dict) or set(declared_checks) != set(checks):
        reasons.append("checks must contain exactly the canonical alert-link keys")
    else:
        for key, expected in checks.items():
            if type(declared_checks.get(key)) is not bool:
                reasons.append(f"checks.{key} must be a boolean")
            elif declared_checks[key] is not expected:
                reasons.append(f"checks.{key} does not match the recomputed result")

    computed_reasons: list[str] = []
    if not required_round_trips:
        computed_reasons.append(
            "alert-link bench did not complete the required acknowledged round trips"
        )
    if not packet_loss_limit:
        computed_reasons.append("alert-link missing-ack rate exceeds the configured limit")
    if not ack_latency_limit:
        computed_reasons.append("alert-link ACK P95 latency exceeds the configured limit")
    if hardware_event and not mode_boundary:
        computed_reasons.append(
            "alert-link hardware mode cannot target a loopback or unresolved address"
        )
    if hardware_event and is_route_bound and not source_route:
        computed_reasons.append(
            "alert-link local source address is outside the expected source CIDR"
        )
    if hardware_event and is_transport_bound and not transport_source_binding:
        computed_reasons.append(
            "alert-link transport socket is not bound to the selected source address"
        )
    if hardware_event and has_transport_observations and not transport_source_observations:
        computed_reasons.append(
            "alert-link ACK samples do not prove the bound sender source address"
        )
    if hardware_event and has_topology_binding and not topology_binding:
        computed_reasons.append(
            "alert-link endpoint or source route is not bound to a verified topology"
        )
    if not correlated_ack:
        computed_reasons.append("correlated alert acknowledgement is not verified")
    if hardware_event and not checks["authenticated_transport"]:
        computed_reasons.append(
            "hardware alert-link evidence requires the authenticated UDP transport"
        )
    if list(reasons_field) != computed_reasons:
        reasons.append("reasons do not match the recomputed threshold result")
    expected_passed = not computed_reasons
    if passed is not expected_passed:
        reasons.append("passed does not match the recomputed threshold result")
    if require_passed and not passed:
        reasons.append("report does not satisfy require_passed")
    return {
        "schema_version": ALERT_LINK_BENCH_SCHEMA_VERSION,
        "event": "alert_link_bench_verified",
        "gate_passed": not reasons,
        "recomputed_passed": expected_passed,
        "checks": checks,
        "reasons": reasons,
    }


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _remote_details(transport: object) -> tuple[str | None, int | None, bool]:
    host_value = getattr(transport, "host", None)
    port_value = getattr(transport, "port", None)
    host = str(host_value).strip() if isinstance(host_value, str) else None
    port = (
        int(port_value)
        if isinstance(port_value, int) and not isinstance(port_value, bool)
        else None
    )
    if not host:
        return None, port, True
    try:
        return host, port, ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname is not assumed to be physical evidence.  Hardware mode
        # requires a concrete non-loopback IPv4/IPv6 endpoint.
        return host, port, True


def _canonical_network(value: str | None) -> str | None:
    if value is None:
        return None
    return str(ipaddress.ip_network(value.strip(), strict=False))


def _address_in_network(address: str | None, network: str | None) -> bool:
    if address is None or network is None:
        return False
    try:
        parsed_address = ipaddress.ip_address(address)
        parsed_network = ipaddress.ip_network(network, strict=False)
    except ValueError:
        return False
    return parsed_address.version == parsed_network.version and parsed_address in parsed_network


def _resolve_local_source_address(host: str, port: int) -> str:
    """Ask the OS which local address would carry a UDP datagram to ``host``.

    UDP ``connect`` performs route selection without transmitting a packet.
    The returned address is bound into v2 hardware reports before the first
    authenticated alert is sent.
    """

    remote = ipaddress.ip_address(host)
    family = socket.AF_INET6 if remote.version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as channel:
        channel.connect((str(remote), port))
        local = channel.getsockname()[0]
    return str(ipaddress.ip_address(local))


def _observed_at_iso(observed_at: Callable[[], datetime]) -> str:
    timestamp = observed_at()
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise ValueError("alert-link bench observation time must include a timezone")
    return timestamp.astimezone(UTC).isoformat()


def _key_from_environment(variable_name: str) -> bytes:
    if not variable_name.strip():
        raise ValueError("--hmac-key-env cannot be empty")
    value = os.environ.get(variable_name)
    if value is None:
        raise ValueError(f"environment variable {variable_name!r} is not set")
    key = value.encode("utf-8")
    if len(key) < 32:
        raise ValueError("alert HMAC key must contain at least 32 UTF-8 bytes")
    return key


def verify_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an alert-link bench report offline.")
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--require-passed",
        action="store_true",
        help="return non-zero unless the report's recomputed thresholds passed",
    )
    args = parser.parse_args(argv)
    try:
        document = strict_json_loads(args.report.read_bytes(), name="alert-link bench report")
        result = verify_alert_link_bench_report(document, require_passed=args.require_passed)
    except (OSError, TypeError, ValueError) as exc:
        result = {
            "schema_version": ALERT_LINK_BENCH_SCHEMA_VERSION,
            "event": "alert_link_bench_verified",
            "gate_passed": False,
            "recomputed_passed": False,
            "checks": {},
            "reasons": [f"unable to load report: {exc}"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["gate_passed"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure authenticated UDP fire-alert ACK round trips. The report is a "
            "transport diagnostic and never enables flight control or payload release."
        )
    )
    parser.add_argument("--topology", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--hmac-key-env", required=True)
    parser.add_argument("--sender-id")
    parser.add_argument("--receiver-id")
    parser.add_argument("--minimum-round-trips", type=int, default=DEFAULT_MINIMUM_ROUND_TRIPS)
    parser.add_argument("--maximum-attempts", type=int, default=DEFAULT_MAXIMUM_ATTEMPTS)
    parser.add_argument("--ack-timeout-seconds", type=float, default=DEFAULT_ACK_TIMEOUT_SECONDS)
    parser.add_argument("--initial-backoff-seconds", type=float, default=0.1)
    parser.add_argument("--maximum-backoff-seconds", type=float, default=5.0)
    parser.add_argument(
        "--maximum-packet-loss-rate",
        type=float,
        default=DEFAULT_MAXIMUM_PACKET_LOSS_RATE,
    )
    parser.add_argument(
        "--maximum-ack-latency-p95-ms",
        type=float,
        default=DEFAULT_MAXIMUM_ACK_LATENCY_P95_MS,
    )
    parser.add_argument("--hardware-mode", action="store_true")
    parser.add_argument("--hardware-id")
    parser.add_argument(
        "--expected-source-cidr",
        help=(
            "required in hardware mode; the OS-selected local UDP source address "
            "must belong to this network"
        ),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        topology = load_alert_link_topology(args.topology) if args.topology is not None else None
    except ValueError as exc:
        parser.error(str(exc))
    if topology is not None:
        for name, supplied, expected in (
            ("--host", args.host, topology.remote_host),
            ("--port", args.port, topology.remote_port),
            ("--sender-id", args.sender_id, topology.sender_id),
            ("--receiver-id", args.receiver_id, topology.receiver_id),
            ("--hardware-id", args.hardware_id, topology.hardware_id),
            ("--expected-source-cidr", args.expected_source_cidr, topology.expected_source_cidr),
        ):
            if supplied is not None and supplied != expected:
                parser.error(f"{name} conflicts with --topology")
        args.host = topology.remote_host
        args.port = topology.remote_port
        args.sender_id = topology.sender_id
        args.receiver_id = topology.receiver_id
        args.hardware_id = topology.hardware_id
        args.expected_source_cidr = topology.expected_source_cidr
        args.hardware_mode = True
    if args.host is None:
        parser.error("--host is required without --topology")
    if args.port is None:
        args.port = 14_600
    if args.sender_id is None:
        args.sender_id = "aircraft-1"
    if args.receiver_id is None:
        args.receiver_id = "ground-station-1"

    config = AlertLinkBenchConfig(
        minimum_round_trips=args.minimum_round_trips,
        maximum_attempts=args.maximum_attempts,
        initial_backoff_seconds=args.initial_backoff_seconds,
        maximum_backoff_seconds=args.maximum_backoff_seconds,
        maximum_packet_loss_rate=args.maximum_packet_loss_rate,
        maximum_ack_latency_p95_ms=args.maximum_ack_latency_p95_ms,
        hardware_mode=args.hardware_mode,
        hardware_id=args.hardware_id,
        expected_source_cidr=args.expected_source_cidr,
        topology=topology,
    )
    source_host: str | None = None
    if config.hardware_mode:
        try:
            source_host = _resolve_local_source_address(args.host, args.port)
        except (OSError, TypeError, ValueError):
            # ``run_alert_link_bench`` repeats the preflight and emits a
            # verifiable zero-transmission failure report. Do not hide the
            # route failure behind an argparse/process exception.
            source_host = None
    transport = UdpAcknowledgedAlertTransport(
        host=args.host,
        port=args.port,
        hmac_key=_key_from_environment(args.hmac_key_env),
        sender_id=args.sender_id,
        receiver_id=args.receiver_id,
        source_host=source_host,
        acknowledgement_timeout_seconds=args.ack_timeout_seconds,
    )
    report = run_alert_link_bench(transport, config)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_name(f".{args.out.name}.{os.getpid()}.tmp")
    temporary.write_text(encoded, encoding="utf-8", newline="\n")
    os.replace(temporary, args.out)
    print(encoded, end="")
    return 0 if report["passed"] else 1


__all__ = [
    "ALERT_LINK_BENCH_SCHEMA_VERSION",
    "SUPPORTED_ALERT_LINK_BENCH_SCHEMA_VERSIONS",
    "AlertLinkBenchConfig",
    "run_alert_link_bench",
    "verify_alert_link_bench_report",
    "verify_main",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

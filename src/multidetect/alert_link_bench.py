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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .alerts import (
    AcknowledgedAlertTransport,
    AlertAcknowledgementError,
    RetryingAcknowledgedAlertPublisher,
    UdpAcknowledgedAlertTransport,
)
from .autonomy.serialization import strict_json_loads
from .compat import UTC
from .domain import BoundingBox, FireAlert

ALERT_LINK_BENCH_SCHEMA_VERSION = 1
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


def run_alert_link_bench(
    transport: AcknowledgedAlertTransport,
    config: AlertLinkBenchConfig,
    *,
    wall_clock: Callable[[], float] = time.time,
    monotonic_clock: Callable[[], float] = time.monotonic,
    observed_at: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Run bounded acknowledged alert probes through ``transport``.

    ``transport`` is the same port used by the live patrol.  A successful
    sample means the receiver returned a matching, validated receipt.  Retry
    attempts that do not receive an acknowledgement are counted in the packet
    loss estimate; a final failed sample never satisfies the required sample
    count.  The optional ``hardware_mode`` flag is an explicit field-session
    assertion and still rejects loopback endpoints.
    """

    session_id = f"alert-link-bench-{uuid4()}"
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
    errors: list[str] = []
    attempt_counts: list[int] = []
    successful_samples: list[bool] = []

    for sequence in range(1, config.minimum_round_trips + 1):
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

    if (
        len(attempt_counts) != config.minimum_round_trips
        or len(successful_samples) != config.minimum_round_trips
    ):
        raise RuntimeError("alert-link bench sample accounting is incomplete")
    remote_host, remote_port, remote_is_loopback = _remote_details(transport)
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
        "hardware_id": config.hardware_id.strip() if config.hardware_id else None,
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

    if document.get("schema_version") != ALERT_LINK_BENCH_SCHEMA_VERSION:
        reasons.append("schema_version must be 1")
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
    hardware_id = document.get("hardware_id")
    if hardware_event:
        if not isinstance(hardware_id, str) or not hardware_id.strip():
            reasons.append("hardware event requires a non-empty hardware_id")
    elif hardware_id is not None:
        reasons.append("software baseline hardware_id must be null")

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

    if requested is not None and samples is not None and failed is not None:
        if samples + failed != requested:
            reasons.append("round_trip_samples plus failed_round_trips must equal requested")
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
        if not authenticated_ack:
            reasons.append("hardware event requires authenticated ACK verification")
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
    required_round_trips = requested is not None and samples == requested
    packet_loss_limit = packet_loss is not None and float(packet_loss) <= float(maximum_loss)
    ack_latency_limit = p95_value is not None and p95_value <= float(maximum_p95)
    checks = {
        "required_round_trips": required_round_trips,
        "packet_loss_limit": packet_loss_limit,
        "ack_latency_limit": ack_latency_limit,
        "mode_boundary": mode_boundary,
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
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=14_600)
    parser.add_argument("--hmac-key-env", required=True)
    parser.add_argument("--sender-id", default="aircraft-1")
    parser.add_argument("--receiver-id", default="ground-station-1")
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
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    transport = UdpAcknowledgedAlertTransport(
        host=args.host,
        port=args.port,
        hmac_key=_key_from_environment(args.hmac_key_env),
        sender_id=args.sender_id,
        receiver_id=args.receiver_id,
        acknowledgement_timeout_seconds=args.ack_timeout_seconds,
    )
    config = AlertLinkBenchConfig(
        minimum_round_trips=args.minimum_round_trips,
        maximum_attempts=args.maximum_attempts,
        initial_backoff_seconds=args.initial_backoff_seconds,
        maximum_backoff_seconds=args.maximum_backoff_seconds,
        maximum_packet_loss_rate=args.maximum_packet_loss_rate,
        maximum_ack_latency_p95_ms=args.maximum_ack_latency_p95_ms,
        hardware_mode=args.hardware_mode,
        hardware_id=args.hardware_id,
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
    "AlertLinkBenchConfig",
    "run_alert_link_bench",
    "verify_alert_link_bench_report",
    "verify_main",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

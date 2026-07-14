from __future__ import annotations

import ipaddress
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from .compat import UTC
from .domain import BoundingBox
from .operator_link import SelectionAction, TargetSelectionCommand, VideoGeometry
from .operator_transport import SelectionDeliveryTimeout


@dataclass(frozen=True, slots=True)
class Gr01BenchConfig:
    minimum_round_trips: int = 100
    command_ttl_seconds: float = 3.0
    maximum_packet_loss_rate: float = 0.01
    maximum_ack_latency_p95_ms: float = 500.0
    hardware_mode: bool = False
    hardware_id: str | None = None

    def __post_init__(self) -> None:
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


def run_gr01_link_bench(
    session: Any,
    geometry: VideoGeometry,
    config: Gr01BenchConfig,
    *,
    wall_clock: Any = time.time,
    observed_at: Any = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    accepted_round_trips = 0
    rejected_round_trips = 0
    timed_out_round_trips = 0
    transmission_attempts = 0
    missing_ack_attempts = 0
    latencies_ms: list[float] = []
    session_id = str(uuid4())
    maximum_attempts = int(getattr(session, "maximum_attempts", 1))
    for sequence in range(1, config.minimum_round_trips + 1):
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

    remote_host = str(session.remote[0])
    try:
        remote_is_loopback = ipaddress.ip_address(remote_host).is_loopback
    except ValueError:
        remote_is_loopback = True
    packet_loss_rate = (
        missing_ack_attempts / transmission_attempts if transmission_attempts else 1.0
    )
    latency_p95_ms = _percentile(latencies_ms, 0.95)
    bidirectional_ip_verified = accepted_round_trips > 0 and (
        not config.hardware_mode or not remote_is_loopback
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
    if not bidirectional_ip_verified:
        reasons.append("GR01 bidirectional IP acknowledgement is not verified")
    if not signed_round_trip:
        reasons.append("signed operator round trip is not verified")
    passed = not reasons
    timestamp = observed_at()
    if timestamp.tzinfo is None:
        raise ValueError("GR01 bench observation time must include a timezone")
    hardware_observed = config.hardware_mode and not remote_is_loopback
    prefix = "gr01" if config.hardware_mode else "gr01_software_baseline"
    return {
        "event": f"{prefix}_bench_{'passed' if passed else 'failed'}",
        "observed_at_utc": timestamp.astimezone(UTC).isoformat(),
        "hardware_observed": hardware_observed,
        "simulation_only": not config.hardware_mode,
        "passed": passed,
        "reasons": reasons,
        "hardware_model": "GR01" if config.hardware_mode else "software_loopback",
        "hardware_id": config.hardware_id.strip() if config.hardware_id else None,
        "remote_is_loopback": remote_is_loopback,
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


__all__ = ["Gr01BenchConfig", "run_gr01_link_bench"]

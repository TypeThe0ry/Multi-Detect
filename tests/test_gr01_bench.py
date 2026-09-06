from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

from multidetect.compat import UTC
from multidetect.gr01_bench import (
    Gr01BenchConfig,
    run_gr01_link_bench,
    verify_gr01_link_bench_report,
)
from multidetect.operator_link import VideoGeometry
from multidetect.operator_protocol import SelectionAck, SelectionAckReason

NOW = datetime(2026, 7, 13, 6, 0, tzinfo=UTC)
GEOMETRY = VideoGeometry("camera-main", 1280, 720)


def _receipt(*, accepted: bool = True, attempts: int = 1, elapsed_s: float = 0.02):
    return SimpleNamespace(
        acknowledgement=SelectionAck(
            command_id="00000000-0000-4000-8000-000000000001",
            accepted=accepted,
            reason=SelectionAckReason.ACCEPTED if accepted else SelectionAckReason.INVALID,
            acknowledged_sequence=1,
        ),
        attempts=attempts,
        elapsed_s=elapsed_s,
    )


class _Session:
    maximum_attempts = 3

    def __init__(self, host: str, receipts: list, *, source_host: str | None = None) -> None:
        self.remote = (host, 14580)
        self.local_source_address = source_host or (
            "127.0.0.1" if host == "127.0.0.1" else "192.168.10.10"
        )
        self.receipts = receipts

    def deliver(self, _command):
        return self.receipts.pop(0)


def test_gr01_hardware_bench_passes_non_loopback_signed_round_trips() -> None:
    result = run_gr01_link_bench(
        _Session("192.168.10.20", [_receipt(), _receipt(elapsed_s=0.03), _receipt()]),
        GEOMETRY,
        Gr01BenchConfig(
            minimum_round_trips=3,
            hardware_mode=True,
            hardware_id="GR01-BENCH-001",
            expected_source_cidr="192.168.10.0/24",
        ),
        wall_clock=lambda: 1000.0,
        observed_at=lambda: NOW,
    )

    assert result["event"] == "gr01_bench_passed"
    assert result["passed"] is True
    assert result["hardware_observed"] is True
    assert result["round_trip_samples"] == 3
    assert result["packet_loss_rate"] == 0
    assert result["ack_latency_p95_ms"] == 30
    assert result["bidirectional_ip_verified"] is True
    assert result["mavlink2_signature_verified"] is True
    assert result["physical_release_enabled"] is False


def test_gr01_hardware_mode_rejects_loopback_even_when_protocol_passes() -> None:
    result = run_gr01_link_bench(
        _Session("127.0.0.1", [_receipt()]),
        GEOMETRY,
        Gr01BenchConfig(
            minimum_round_trips=1,
            hardware_mode=True,
            hardware_id="GR01-BENCH-001",
            expected_source_cidr="192.168.10.0/24",
        ),
        wall_clock=lambda: 1000.0,
        observed_at=lambda: NOW,
    )

    assert result["event"] == "gr01_bench_failed"
    assert result["passed"] is False
    assert result["hardware_observed"] is False
    assert "cannot target a loopback address" in " ".join(result["reasons"])


def test_gr01_software_loopback_is_labeled_as_simulation_only() -> None:
    result = run_gr01_link_bench(
        _Session("127.0.0.1", [_receipt()]),
        GEOMETRY,
        Gr01BenchConfig(minimum_round_trips=1),
        wall_clock=lambda: 1000.0,
        observed_at=lambda: NOW,
    )

    assert result["event"] == "gr01_software_baseline_bench_passed"
    assert result["passed"] is True
    assert result["hardware_observed"] is False
    assert result["simulation_only"] is True
    assert result["hardware_model"] == "software_loopback"


def test_gr01_bench_fails_packet_loss_and_latency_limits() -> None:
    result = run_gr01_link_bench(
        _Session(
            "192.168.10.20",
            [_receipt(attempts=2, elapsed_s=0.7), _receipt(), _receipt()],
        ),
        GEOMETRY,
        Gr01BenchConfig(
            minimum_round_trips=3,
            hardware_mode=True,
            hardware_id="GR01-BENCH-001",
            expected_source_cidr="192.168.10.0/24",
            maximum_packet_loss_rate=0.01,
            maximum_ack_latency_p95_ms=500,
        ),
        wall_clock=lambda: 1000.0,
        observed_at=lambda: NOW,
    )

    assert result["passed"] is False
    assert result["packet_loss_rate"] == 0.25
    assert result["ack_latency_p95_ms"] == 700
    assert "packet-loss rate" in " ".join(result["reasons"])
    assert "P95 latency" in " ".join(result["reasons"])


def test_gr01_hardware_bench_blocks_transmission_on_wrong_source_route() -> None:
    session = _Session(
        "192.168.10.20",
        [_receipt()],
        source_host="10.20.30.40",
    )
    result = run_gr01_link_bench(
        session,
        GEOMETRY,
        Gr01BenchConfig(
            minimum_round_trips=1,
            hardware_mode=True,
            hardware_id="GR01-BENCH-001",
            expected_source_cidr="192.168.10.0/24",
        ),
        wall_clock=lambda: 1000.0,
        observed_at=lambda: NOW,
    )

    assert result["passed"] is False
    assert result["source_route_verified"] is False
    assert result["transmission_attempts"] == 0
    assert session.receipts


def _passing_hardware_report() -> dict:
    return run_gr01_link_bench(
        _Session("192.168.10.20", [_receipt()]),
        GEOMETRY,
        Gr01BenchConfig(
            minimum_round_trips=1,
            hardware_mode=True,
            hardware_id="GR01-BENCH-001",
            expected_source_cidr="192.168.10.0/24",
        ),
        wall_clock=lambda: 1000.0,
        observed_at=lambda: NOW,
    )


def test_gr01_bench_verifier_accepts_consistent_passing_report() -> None:
    result = verify_gr01_link_bench_report(_passing_hardware_report(), require_passed=True)

    assert result["gate_passed"] is True
    assert result["recomputed_passed"] is True
    assert all(result["checks"].values())


def test_gr01_bench_verifier_rejects_forged_source_route_flag() -> None:
    forged = deepcopy(_passing_hardware_report())
    forged["source_route_verified"] = False

    result = verify_gr01_link_bench_report(forged, require_passed=True)

    assert result["gate_passed"] is False
    assert result["checks"]["source_route_flag"] is False


def test_gr01_bench_verifier_accepts_consistent_failed_report_without_pass_gate() -> None:
    report = run_gr01_link_bench(
        _Session("192.168.10.20", [_receipt()], source_host="10.20.30.40"),
        GEOMETRY,
        Gr01BenchConfig(
            minimum_round_trips=1,
            hardware_mode=True,
            hardware_id="GR01-BENCH-001",
            expected_source_cidr="192.168.10.0/24",
        ),
        wall_clock=lambda: 1000.0,
        observed_at=lambda: NOW,
    )

    result = verify_gr01_link_bench_report(report)

    assert result["gate_passed"] is True
    assert result["recomputed_passed"] is False

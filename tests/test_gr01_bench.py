from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from multidetect.compat import UTC
from multidetect.gr01_bench import Gr01BenchConfig, run_gr01_link_bench
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

    def __init__(self, host: str, receipts: list) -> None:
        self.remote = (host, 14580)
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

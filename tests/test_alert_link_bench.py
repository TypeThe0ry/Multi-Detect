from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from multidetect.alert_link_bench import (
    AlertLinkBenchConfig,
    run_alert_link_bench,
    verify_alert_link_bench_report,
    verify_main,
)
from multidetect.alert_link_topology import AlertLinkTopology
from multidetect.alerts import (
    AlertDeliveryReceipt,
    AuthenticatedUdpAlertReceiver,
    UdpAcknowledgedAlertTransport,
)
from multidetect.compat import UTC

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


class _ScriptedTransport:
    host = "127.0.0.1"
    port = 14600
    sender_id = "aircraft-1"
    receiver_id = "ground-station-1"

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def send(self, document: dict[str, object]) -> AlertDeliveryReceipt:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return AlertDeliveryReceipt(
            alert_id=str(document["alert_id"]),
            receiver_id="ground-station-1",
            acknowledged_at_s=1.0,
        )


def _config(**overrides: object) -> AlertLinkBenchConfig:
    values: dict[str, object] = {
        "minimum_round_trips": 3,
        "maximum_attempts": 3,
        "initial_backoff_seconds": 0.0,
        "maximum_backoff_seconds": 0.0,
    }
    values.update(overrides)
    if (
        values.get("hardware_mode") is True
        and "topology" not in values
        and isinstance(values.get("expected_source_cidr"), str)
        and isinstance(values.get("hardware_id"), str)
    ):
        values["topology"] = AlertLinkTopology(
            topology_id="test-gr01-direct",
            mode="gr01_direct",
            remote_host="192.168.144.11",
            remote_port=14600,
            expected_source_cidr=str(values["expected_source_cidr"]),
            sender_id="aircraft-1",
            receiver_id="ground-station-1",
            hardware_id=str(values["hardware_id"]),
        )
    return AlertLinkBenchConfig(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("minimum_round_trips", 0),
        ("maximum_attempts", 0),
        ("initial_backoff_seconds", -1.0),
        ("maximum_backoff_seconds", -1.0),
        ("maximum_packet_loss_rate", 1.1),
        ("maximum_ack_latency_p95_ms", 0.0),
    ],
)
def test_alert_link_bench_config_rejects_invalid_thresholds(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        AlertLinkBenchConfig(**{field: value})


def test_alert_link_software_baseline_reports_correlated_round_trips() -> None:
    transport = _ScriptedTransport(
        [
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
        ]
    )
    result = run_alert_link_bench(
        transport,
        _config(),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: 10.0,
        observed_at=lambda: NOW,
    )

    assert result["schema_version"] == 5
    assert result["event"] == "alert_link_software_baseline_bench_passed"
    assert result["passed"] is True
    assert result["simulation_only"] is True
    assert result["hardware_observed"] is False
    assert result["round_trip_samples"] == 3
    assert result["transmission_attempts"] == 3
    assert result["packet_loss_rate"] == 0
    assert result["correlated_ack_verified"] is True
    assert result["authenticated_ack_verified"] is False
    assert result["flight_control_enabled"] is False
    assert transport.calls == 3

    schema = json.loads(
        (ROOT / "configs" / "schemas" / "alert-link-bench.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(result)


def test_alert_link_bench_counts_retry_loss_and_fails_threshold() -> None:
    transport = _ScriptedTransport(
        [
            TimeoutError("drop"),
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
            TimeoutError("drop"),
            TimeoutError("drop"),
            TimeoutError("drop"),
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
        ]
    )
    result = run_alert_link_bench(
        transport,
        _config(maximum_packet_loss_rate=0.1),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: 10.0,
        observed_at=lambda: NOW,
    )

    assert result["passed"] is False
    assert result["round_trip_samples"] == 2
    assert result["failed_round_trips"] == 1
    assert result["transmission_attempts"] == 6
    assert result["missing_ack_attempts"] == 4
    assert result["packet_loss_rate"] == pytest.approx(4 / 6)
    assert "missing-ack rate" in " ".join(result["reasons"])
    assert result["failure_types"] == {"AlertAcknowledgementError": 1}


def test_alert_link_hardware_mode_rejects_loopback_and_non_authenticated_transport() -> None:
    transport = _ScriptedTransport([AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0)])
    result = run_alert_link_bench(
        transport,
        _config(
            minimum_round_trips=1,
            hardware_mode=True,
            hardware_id="GR01-ALERT-001",
            expected_source_cidr="192.168.144.0/24",
        ),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: 10.0,
        observed_at=lambda: NOW,
    )

    assert result["passed"] is False
    assert result["hardware_observed"] is False
    assert result["simulation_only"] is False
    reasons = " ".join(result["reasons"])
    assert "loopback" in reasons
    assert "authenticated UDP" in reasons


def test_alert_link_hardware_mode_requires_non_loopback_source_network() -> None:
    with pytest.raises(ValueError, match="expected source CIDR"):
        _config(hardware_mode=True, hardware_id="GR01-ALERT-001")
    with pytest.raises(ValueError, match="cannot be loopback"):
        _config(
            hardware_mode=True,
            hardware_id="GR01-ALERT-001",
            expected_source_cidr="127.0.0.0/8",
        )


def test_alert_link_hardware_mode_binds_and_checks_selected_source_route() -> None:
    transport = _ScriptedTransport([AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0)])
    transport.host = "192.168.144.11"
    result = run_alert_link_bench(
        transport,
        _config(
            minimum_round_trips=1,
            hardware_mode=True,
            hardware_id="GR01-ALERT-001",
            expected_source_cidr="192.168.144.0/24",
        ),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: 10.0,
        observed_at=lambda: NOW,
        source_address_resolver=lambda _host, _port: "192.168.1.13",
    )

    assert result["schema_version"] == 5
    assert result["expected_source_cidr"] == "192.168.144.0/24"
    assert result["local_source_address"] == "192.168.1.13"
    assert result["source_route_verified"] is False
    assert result["checks"]["source_route"] is False
    assert result["transport_source_address"] is None
    assert result["transport_source_address_bound"] is False
    assert result["checks"]["transport_source_binding"] is False
    assert result["observed_transport_source_addresses"] == []
    assert result["transport_source_observation_count"] == 0
    assert result["transport_source_observations_complete"] is False
    assert result["checks"]["transport_source_observations"] is False
    assert result["passed"] is False
    assert result["transmission_attempts"] == 0
    assert transport.calls == 0
    assert "outside the expected source CIDR" in " ".join(result["reasons"])

    verified = verify_alert_link_bench_report(result)
    assert verified["gate_passed"] is True
    assert verified["recomputed_passed"] is False


def test_alert_link_hardware_mode_requires_transport_pinned_to_selected_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = UdpAcknowledgedAlertTransport(
        host="192.168.144.11",
        port=14_600,
        hmac_key=b"test-only-alert-key-material-32-bytes-minimum",
        sender_id="aircraft-1",
        receiver_id="ground-station-1",
        source_host="192.168.144.20",
    )
    calls = 0

    def acknowledge(document: dict[str, object]) -> AlertDeliveryReceipt:
        nonlocal calls
        calls += 1
        return AlertDeliveryReceipt(
            str(document["alert_id"]),
            "ground-station-1",
            10.001,
            source_address="192.168.144.20",
        )

    monkeypatch.setattr(transport, "send", acknowledge)
    monotonic_values = iter((10.0, 10.001))
    result = run_alert_link_bench(
        transport,
        _config(
            minimum_round_trips=1,
            hardware_mode=True,
            hardware_id="GR01-ALERT-001",
            expected_source_cidr="192.168.144.0/24",
        ),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: next(monotonic_values),
        observed_at=lambda: NOW,
        source_address_resolver=lambda _host, _port: "192.168.144.20",
    )

    assert calls == 1
    assert result["passed"] is True
    assert result["source_route_verified"] is True
    assert result["transport_source_address"] == "192.168.144.20"
    assert result["transport_source_address_bound"] is True
    assert result["checks"]["transport_source_binding"] is True
    assert result["observed_transport_source_addresses"] == ["192.168.144.20"]
    assert result["transport_source_observation_count"] == 1
    assert result["transport_source_observations_complete"] is True
    assert result["checks"]["transport_source_observations"] is True
    assert verify_alert_link_bench_report(result, require_passed=True)["gate_passed"] is True


def test_alert_link_hardware_mode_rejects_ack_without_observed_sender_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = UdpAcknowledgedAlertTransport(
        host="192.168.144.11",
        port=14_600,
        hmac_key=b"test-only-alert-key-material-32-bytes-minimum",
        sender_id="aircraft-1",
        receiver_id="ground-station-1",
        source_host="192.168.144.20",
    )

    def acknowledge(document: dict[str, object]) -> AlertDeliveryReceipt:
        return AlertDeliveryReceipt(str(document["alert_id"]), "ground-station-1", 10.001)

    monkeypatch.setattr(transport, "send", acknowledge)
    monotonic_values = iter((10.0, 10.001))
    result = run_alert_link_bench(
        transport,
        _config(
            minimum_round_trips=1,
            hardware_mode=True,
            hardware_id="GR01-ALERT-001",
            expected_source_cidr="192.168.144.0/24",
        ),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: next(monotonic_values),
        observed_at=lambda: NOW,
        source_address_resolver=lambda _host, _port: "192.168.144.20",
    )

    assert result["round_trip_samples"] == 1
    assert result["authenticated_ack_verified"] is True
    assert result["transport_source_address_bound"] is True
    assert result["transport_source_observation_count"] == 0
    assert result["transport_source_observations_complete"] is False
    assert result["checks"]["transport_source_observations"] is False
    assert result["passed"] is False
    verified = verify_alert_link_bench_report(result)
    assert verified["gate_passed"] is True
    assert verified["recomputed_passed"] is False


@pytest.mark.usefixtures("require_loopback_sockets")
def test_authenticated_udp_alert_link_bench_exercises_real_loopback_socket() -> None:
    key = b"alert-link-bench-test-key-material-32-bytes"
    received: list[object] = []
    with AuthenticatedUdpAlertReceiver(
        bind_host="127.0.0.1",
        port=0,
        hmac_key=key,
        receiver_id="ground-station-1",
        expected_sender_id="aircraft-1",
        receive_timeout_seconds=2.0,
    ) as receiver:
        worker = threading.Thread(
            target=lambda: received.extend(receiver.receive() for _ in range(3)),
            daemon=True,
        )
        worker.start()
        transport = UdpAcknowledgedAlertTransport(
            host="127.0.0.1",
            port=receiver.local_address[1],
            hmac_key=key,
            sender_id="aircraft-1",
            receiver_id="ground-station-1",
            acknowledgement_timeout_seconds=1.0,
        )
        result = run_alert_link_bench(
            transport,
            _config(),
            observed_at=lambda: NOW,
        )
        worker.join(timeout=2.0)

    assert worker.is_alive() is False
    assert len(received) == 3
    assert result["event"] == "alert_link_software_baseline_bench_passed"
    assert result["passed"] is True
    assert result["transport_protocol"] == "authenticated_udp"
    assert result["authenticated_ack_verified"] is True
    assert result["correlated_ack_verified"] is True
    assert result["remote_is_loopback"] is True
    assert result["expected_source_cidr"] is None
    assert result["local_source_address"] is None
    assert result["source_route_verified"] is False
    assert result["checks"]["source_route"] is True
    assert result["transport_source_address"] is None
    assert result["transport_source_address_bound"] is False
    assert result["checks"]["transport_source_binding"] is True
    assert result["observed_transport_source_addresses"] == ["127.0.0.1"] * 3
    assert result["transport_source_observation_count"] == 3
    assert result["transport_source_observations_complete"] is True
    assert result["checks"]["transport_source_observations"] is True


def test_alert_link_bench_verifier_recomputes_report_and_rejects_tampering(tmp_path) -> None:
    transport = _ScriptedTransport(
        [
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
        ]
    )
    report = run_alert_link_bench(
        transport,
        _config(),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: 10.0,
        observed_at=lambda: NOW,
    )

    verified = verify_alert_link_bench_report(report, require_passed=True)
    assert verified["gate_passed"] is True
    assert verified["recomputed_passed"] is True

    tampered = dict(report)
    tampered["packet_loss_rate"] = 0.5
    tampered_result = verify_alert_link_bench_report(tampered)
    assert tampered_result["gate_passed"] is False
    assert "packet_loss_rate does not match" in " ".join(tampered_result["reasons"])

    path = tmp_path / "alert-link-bench.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(report).encode("utf-8"))
    assert verify_main([str(path), "--require-passed"]) == 0


def test_alert_link_bench_verifier_keeps_legacy_v1_reports_readable() -> None:
    transport = _ScriptedTransport([AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0)])
    report = run_alert_link_bench(
        transport,
        _config(minimum_round_trips=1),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: 10.0,
        observed_at=lambda: NOW,
    )
    legacy = dict(report)
    legacy["schema_version"] = 1
    legacy["checks"] = {
        key: value
        for key, value in report["checks"].items()
        if key
        not in {
            "source_route",
            "transport_source_binding",
            "transport_source_observations",
            "topology_binding",
        }
    }
    for field in (
        "expected_source_cidr",
        "local_source_address",
        "source_route_verified",
        "transport_source_address",
        "transport_source_address_bound",
        "observed_transport_source_addresses",
        "transport_source_observation_count",
        "transport_source_observations_complete",
        "topology",
        "topology_sha256",
        "topology_binding_verified",
    ):
        legacy.pop(field)

    verified = verify_alert_link_bench_report(legacy, require_passed=True)
    assert verified["gate_passed"] is True
    assert verified["recomputed_passed"] is True


def test_alert_link_bench_verifier_keeps_route_bound_v2_reports_readable() -> None:
    transport = _ScriptedTransport([AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0)])
    report = run_alert_link_bench(
        transport,
        _config(minimum_round_trips=1),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: 10.0,
        observed_at=lambda: NOW,
    )
    legacy = dict(report)
    legacy["schema_version"] = 2
    legacy["checks"] = {
        key: value
        for key, value in report["checks"].items()
        if key
        not in {
            "transport_source_binding",
            "transport_source_observations",
            "topology_binding",
        }
    }
    legacy.pop("transport_source_address")
    legacy.pop("transport_source_address_bound")
    legacy.pop("observed_transport_source_addresses")
    legacy.pop("transport_source_observation_count")
    legacy.pop("transport_source_observations_complete")
    legacy.pop("topology")
    legacy.pop("topology_sha256")
    legacy.pop("topology_binding_verified")

    verified = verify_alert_link_bench_report(legacy, require_passed=True)
    assert verified["gate_passed"] is True
    assert verified["recomputed_passed"] is True


def test_alert_link_bench_verifier_keeps_sender_bound_v3_reports_readable() -> None:
    transport = _ScriptedTransport([AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0)])
    report = run_alert_link_bench(
        transport,
        _config(minimum_round_trips=1),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: 10.0,
        observed_at=lambda: NOW,
    )
    legacy = dict(report)
    legacy["schema_version"] = 3
    legacy["checks"] = {
        key: value
        for key, value in report["checks"].items()
        if key not in {"transport_source_observations", "topology_binding"}
    }
    legacy.pop("observed_transport_source_addresses")
    legacy.pop("transport_source_observation_count")
    legacy.pop("transport_source_observations_complete")
    legacy.pop("topology")
    legacy.pop("topology_sha256")
    legacy.pop("topology_binding_verified")

    verified = verify_alert_link_bench_report(legacy, require_passed=True)
    assert verified["gate_passed"] is True
    assert verified["recomputed_passed"] is True


def test_alert_link_bench_verifier_recomputes_endpoint_and_handles_malformed_event() -> None:
    transport = _ScriptedTransport(
        [
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
            AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0),
        ]
    )
    report = run_alert_link_bench(
        transport,
        _config(),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: 10.0,
        observed_at=lambda: NOW,
    )

    endpoint_tampered = dict(report)
    endpoint_tampered["remote_host"] = "127.0.0.1"
    endpoint_tampered["remote_is_loopback"] = False
    endpoint_result = verify_alert_link_bench_report(endpoint_tampered)
    assert endpoint_result["gate_passed"] is False
    assert "remote_is_loopback does not match" in " ".join(endpoint_result["reasons"])

    malformed_event = dict(report)
    malformed_event["event"] = []
    malformed_result = verify_alert_link_bench_report(malformed_event)
    assert malformed_result["gate_passed"] is False
    assert "event is not a supported" in " ".join(malformed_result["reasons"])


def test_alert_link_bench_verifier_rejects_tampered_topology_hash() -> None:
    transport = _ScriptedTransport([AlertDeliveryReceipt("placeholder", "ground-station-1", 1.0)])
    transport.host = "192.168.144.11"
    result = run_alert_link_bench(
        transport,
        _config(
            minimum_round_trips=1,
            hardware_mode=True,
            hardware_id="GR01-ALERT-001",
            expected_source_cidr="192.168.144.0/24",
        ),
        wall_clock=lambda: 100.0,
        monotonic_clock=lambda: 10.0,
        observed_at=lambda: NOW,
        source_address_resolver=lambda _host, _port: "192.168.144.13",
    )
    tampered = dict(result)
    tampered["topology_sha256"] = "0" * 64
    verification = verify_alert_link_bench_report(tampered)
    assert verification["gate_passed"] is False
    assert "topology" in " ".join(verification["reasons"])


def test_alert_link_bench_verifier_rejects_duplicate_json_keys(tmp_path, capsys) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")

    assert verify_main([str(path)]) == 1
    assert "duplicate JSON key" in capsys.readouterr().out

from __future__ import annotations

import struct

import pytest

from multidetect.pixhawk_link_audit import (
    V6XLinkAuditExpectations,
    audit_v6x_link_topology,
)
from multidetect.pixhawk_parameters import PixhawkParameterRecord, PixhawkParameterSnapshot


def _snapshot(values: dict[str, int]) -> PixhawkParameterSnapshot:
    parameters = tuple(
        PixhawkParameterRecord(
            name=name,
            value=value,
            raw_value_hex=struct.pack("<i", value).hex(),
            parameter_type=6,
            index=index,
        )
        for index, (name, value) in enumerate(values.items())
    )
    return PixhawkParameterSnapshot(
        captured_at_utc="2026-07-14T00:00:00+00:00",
        configured_endpoint="tcp:192.168.144.11:5760",
        resolved_endpoint="tcp:192.168.144.11:5760",
        parameter_encoding="bytewise",
        target_system_id=1,
        target_component_id=1,
        duration_seconds=1.0,
        expected_parameter_count=len(parameters),
        received_parameter_count=len(parameters),
        rejected_source_message_count=0,
        invalid_parameter_message_count=0,
        active_read_requests_transmitted=1,
        px4_parameter_hash_raw_hex=None,
        parameters=parameters,
        complete=True,
        passed=True,
        failure_reasons=(),
    )


def _current_aircraft_values() -> dict[str, int]:
    return {
        "MAV_0_CONFIG": 101,
        "MAV_0_MODE": 0,
        "MAV_0_RATE": 1200,
        "MAV_0_FORWARD": 1,
        "MAV_1_CONFIG": 0,
        "MAV_2_CONFIG": 1000,
        "MAV_2_MODE": 0,
        "MAV_2_RATE": 100000,
        "MAV_2_FORWARD": 0,
        "MAV_2_BROADCAST": 1,
        "MAV_2_REMOTE_PRT": 14550,
        "MAV_2_UDP_PRT": 14550,
        "SER_TEL1_BAUD": 115200,
    }


def test_current_v6x_configuration_passes_primary_links_and_reports_uart_not_ready() -> None:
    report = audit_v6x_link_topology(_snapshot(_current_aircraft_values()))

    assert report["gate_passed"] is True
    assert report["primary_configuration_passed"] is True
    assert report["uart_fallback_ready"] is False
    assert report["uart_fallback_required"] is False
    assert report["links"]["gr01_v6x_telem1"]["configured_baud"] == 115200
    assert report["links"]["jetson_v6x_primary"]["listen_endpoint"] == "udp:0.0.0.0:14550"
    assert report["links"]["jetson_v6x_primary"]["baud_applies"] is False
    assert report["links"]["jetson_v6x_uart_fallback"]["ready"] is False
    assert report["warnings"]
    assert report["messages_transmitted"] == 0
    assert report["hardware_contacted"] is False


def test_uart_fallback_can_be_required_without_changing_primary_result() -> None:
    report = audit_v6x_link_topology(
        _snapshot(_current_aircraft_values()),
        V6XLinkAuditExpectations(require_uart_fallback=True),
    )

    assert report["primary_configuration_passed"] is True
    assert report["gate_passed"] is False
    assert any("TELEM2" in reason for reason in report["gate_failures"])


def test_fully_configured_uart_fallback_passes_when_required() -> None:
    values = _current_aircraft_values()
    values["MAV_1_CONFIG"] = 102
    values["MAV_1_MODE"] = 2
    values["MAV_1_RATE"] = 0
    values["SER_TEL2_BAUD"] = 921600

    report = audit_v6x_link_topology(
        _snapshot(values),
        V6XLinkAuditExpectations(require_uart_fallback=True),
    )

    assert report["gate_passed"] is True
    assert report["uart_fallback_ready"] is True
    assert report["warnings"] == []


@pytest.mark.parametrize(
    ("name", "value", "expected_failure"),
    [
        ("SER_TEL1_BAUD", 57600, "SER_TEL1_BAUD"),
        ("MAV_2_UDP_PRT", 14556, "MAV_2_UDP_PRT"),
        ("MAV_2_REMOTE_PRT", 14556, "MAV_2_REMOTE_PRT"),
        ("MAV_2_BROADCAST", 0, "MAV_2_BROADCAST"),
    ],
)
def test_primary_configuration_mismatches_fail_closed(
    name: str, value: int, expected_failure: str
) -> None:
    values = _current_aircraft_values()
    values[name] = value

    report = audit_v6x_link_topology(_snapshot(values))

    assert report["gate_passed"] is False
    assert any(expected_failure in reason for reason in report["gate_failures"])


def test_duplicate_active_port_assignments_are_rejected() -> None:
    values = _current_aircraft_values()
    values["MAV_1_CONFIG"] = 1000

    report = audit_v6x_link_topology(_snapshot(values))

    assert report["gate_passed"] is False
    assert any("duplicated" in reason for reason in report["gate_failures"])


@pytest.mark.parametrize(
    "changes",
    [
        {"gr01_telem1_baud": 0},
        {"jetson_uart_telem2_baud": True},
        {"ethernet_udp_port": 65536},
    ],
)
def test_link_audit_expectations_reject_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        V6XLinkAuditExpectations(**changes)  # type: ignore[arg-type]

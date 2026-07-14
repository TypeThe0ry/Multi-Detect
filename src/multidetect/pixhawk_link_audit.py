from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .pixhawk_parameters import PixhawkParameterSnapshot

_MAVLINK_PORT_NAMES = {
    0: "disabled",
    6: "uart6",
    101: "telem1",
    102: "telem2",
    103: "telem3",
    104: "telem_serial4",
    201: "gps1",
    202: "gps2",
    203: "gps3",
    300: "radio_controller",
    301: "wifi",
    1000: "ethernet",
}
_MAVLINK_INSTANCE_PATTERN = re.compile(r"MAV_(\d+)_CONFIG")


@dataclass(frozen=True, slots=True)
class V6XLinkAuditExpectations:
    """Expected link split for the Holybro Pixhawk Jetson Baseboard installation."""

    gr01_telem1_baud: int = 115_200
    jetson_uart_telem2_baud: int = 921_600
    ethernet_udp_port: int = 14_550
    require_uart_fallback: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("GR01 TELEM1 baud", self.gr01_telem1_baud),
            ("Jetson TELEM2 baud", self.jetson_uart_telem2_baud),
            ("Ethernet UDP port", self.ethernet_udp_port),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.ethernet_udp_port > 65_535:
            raise ValueError("Ethernet UDP port must be in [1, 65535]")


def audit_v6x_link_topology(
    snapshot: PixhawkParameterSnapshot,
    expectations: V6XLinkAuditExpectations | None = None,
) -> dict[str, object]:
    """Offline-audit the three independent V6X transport roles from a verified snapshot.

    This function never opens a MAVLink transport. It proves only configuration consistency;
    physical Ethernet, UART and GR01 connectivity still require separate bench evidence.
    """

    expected = expectations or V6XLinkAuditExpectations()
    values = {record.name: record.value for record in snapshot.parameters}
    instances = _mavlink_instances(values)

    gr01_failures: list[str] = []
    ethernet_failures: list[str] = []
    assignment_failures: list[str] = []
    fallback_failures: list[str] = []
    warnings: list[str] = []

    telem1_instances = _instances_for_port(instances, 101)
    telem2_instances = _instances_for_port(instances, 102)
    ethernet_instances = _instances_for_port(instances, 1000)

    if len(telem1_instances) != 1:
        gr01_failures.append(
            "exactly one MAVLink instance must be mapped to TELEM1 for the GR01 data link"
        )
    telem1_baud = _optional_int(values, "SER_TEL1_BAUD")
    if telem1_baud != expected.gr01_telem1_baud:
        gr01_failures.append(f"SER_TEL1_BAUD must be {expected.gr01_telem1_baud} for the GR01 link")

    if len(ethernet_instances) != 1:
        ethernet_failures.append(
            "exactly one MAVLink instance must be mapped to Ethernet for the Jetson primary link"
        )
        ethernet_instance = None
    else:
        ethernet_instance = ethernet_instances[0]
        for suffix, required_value in (
            ("UDP_PRT", expected.ethernet_udp_port),
            ("REMOTE_PRT", expected.ethernet_udp_port),
            ("BROADCAST", 1),
        ):
            name = f"MAV_{ethernet_instance['instance']}_{suffix}"
            if _optional_int(values, name) != required_value:
                ethernet_failures.append(f"{name} must be {required_value}")

    duplicate_ports = _duplicate_active_ports(instances)
    if duplicate_ports:
        assignment_failures.append(
            "active MAVLink port assignments are duplicated: " + ", ".join(duplicate_ports)
        )

    primary_failures = [*gr01_failures, *ethernet_failures, *assignment_failures]

    telem2_baud = _optional_int(values, "SER_TEL2_BAUD")
    if len(telem2_instances) != 1:
        fallback_failures.append(
            "no single MAVLink instance is mapped to TELEM2 for the optional Jetson UART fallback"
        )
    if telem2_baud != expected.jetson_uart_telem2_baud:
        fallback_failures.append(
            "SER_TEL2_BAUD must be "
            f"{expected.jetson_uart_telem2_baud} for the optional UART fallback"
        )

    if fallback_failures and not expected.require_uart_fallback:
        warnings.extend(fallback_failures)
    gate_failures = list(primary_failures)
    if expected.require_uart_fallback:
        gate_failures.extend(fallback_failures)

    return {
        "schema_version": 1,
        "event": "pixhawk_v6x_link_topology_audited",
        "source_parameter_list_sha256": snapshot.parameter_list_sha256,
        "source_target_system_id": snapshot.target_system_id,
        "source_target_component_id": snapshot.target_component_id,
        "link_planes_independent": True,
        "configuration_only": True,
        "physical_connectivity_verified": False,
        "links": {
            "gr01_v6x_telem1": {
                "role": "ground_control_telemetry",
                "transport": "telem1_uart",
                "mavlink_instances": [item["instance"] for item in telem1_instances],
                "configured_baud": telem1_baud,
                "expected_baud": expected.gr01_telem1_baud,
                "configured": not gr01_failures,
                "part_of_jetson_v6x_transport": False,
            },
            "jetson_v6x_primary": {
                "role": "companion_read_only_telemetry",
                "transport": "board_internal_ethernet_udp",
                "mavlink_instances": [item["instance"] for item in ethernet_instances],
                "listen_endpoint": f"udp:0.0.0.0:{expected.ethernet_udp_port}",
                "baud_applies": False,
                "configured": not ethernet_failures and not assignment_failures,
            },
            "jetson_v6x_uart_fallback": {
                "role": "optional_companion_read_only_telemetry",
                "transport": "board_internal_uart1_telem2",
                "mavlink_instances": [item["instance"] for item in telem2_instances],
                "serial_endpoint": "/dev/ttyTHS1",
                "configured_baud": telem2_baud,
                "expected_baud": expected.jetson_uart_telem2_baud,
                "required": expected.require_uart_fallback,
                "ready": not fallback_failures,
            },
        },
        "mavlink_instances": instances,
        "primary_configuration_passed": not primary_failures,
        "uart_fallback_ready": not fallback_failures,
        "uart_fallback_required": expected.require_uart_fallback,
        "warnings": warnings,
        "gate_passed": not gate_failures,
        "gate_failures": gate_failures,
        "messages_transmitted": 0,
        "parameter_read_requests_transmitted": 0,
        "parameter_write_messages_transmitted": 0,
        "flight_command_messages_transmitted": 0,
        "mission_messages_transmitted": 0,
        "actuator_messages_transmitted": 0,
        "hardware_contacted": False,
        "hardware_control_enabled": False,
    }


def _mavlink_instances(values: dict[str, int | float]) -> list[dict[str, object]]:
    instances: list[dict[str, object]] = []
    for name, raw_value in values.items():
        match = _MAVLINK_INSTANCE_PATTERN.fullmatch(name)
        if match is None:
            continue
        config = _required_int(raw_value, name)
        instance = int(match.group(1))
        fields: dict[str, int] = {}
        for suffix in (
            "MODE",
            "RATE",
            "FORWARD",
            "BROADCAST",
            "REMOTE_PRT",
            "UDP_PRT",
        ):
            field_name = f"MAV_{instance}_{suffix}"
            field_value = _optional_int(values, field_name)
            if field_value is not None:
                fields[suffix.lower()] = field_value
        instances.append(
            {
                "instance": instance,
                "config_value": config,
                "port": _MAVLINK_PORT_NAMES.get(config, f"unknown_{config}"),
                "active": config != 0,
                **fields,
            }
        )
    return sorted(instances, key=lambda item: int(item["instance"]))


def _instances_for_port(
    instances: list[dict[str, object]], config_value: int
) -> list[dict[str, object]]:
    return [item for item in instances if item["config_value"] == config_value]


def _duplicate_active_ports(instances: list[dict[str, object]]) -> list[str]:
    assignments: dict[int, list[int]] = {}
    for item in instances:
        config = int(item["config_value"])
        if config == 0:
            continue
        assignments.setdefault(config, []).append(int(item["instance"]))
    return [
        f"{_MAVLINK_PORT_NAMES.get(config, config)} on instances {','.join(map(str, indices))}"
        for config, indices in sorted(assignments.items())
        if len(indices) > 1
    ]


def _optional_int(values: dict[str, int | float], name: str) -> int | None:
    if name not in values:
        return None
    return _required_int(values[name], name)


def _required_int(value: int | float, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"PX4 link parameter {name} must be numeric")
    if not math.isfinite(float(value)) or int(value) != value:
        raise ValueError(f"PX4 link parameter {name} must be an integer")
    return int(value)


__all__ = [
    "V6XLinkAuditExpectations",
    "audit_v6x_link_topology",
]

from __future__ import annotations

import argparse
import json
import struct
import time
from typing import Any

from pymavlink import mavutil

PARAMETER_NAME = "SER_TEL1_BAUD"
SUPPORTED_BAUDS = (57600, 115200, 921600)


class RepairError(RuntimeError):
    pass


def _parameter_id(message: Any) -> str:
    value = message.param_id
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).split(b"\0", 1)[0].decode("ascii", errors="replace")
    return str(value).split("\0", 1)[0]


def _decode_parameter_value(raw_value: float, parameter_type: int) -> float | int:
    """Decode MAVLink byte-wise integer parameter encoding used by PX4."""
    packed = struct.pack(">f", float(raw_value))
    formats = {
        mavutil.mavlink.MAV_PARAM_TYPE_UINT8: ">xxxB",
        mavutil.mavlink.MAV_PARAM_TYPE_INT8: ">xxxb",
        mavutil.mavlink.MAV_PARAM_TYPE_UINT16: ">xxH",
        mavutil.mavlink.MAV_PARAM_TYPE_INT16: ">xxh",
        mavutil.mavlink.MAV_PARAM_TYPE_UINT32: ">I",
        mavutil.mavlink.MAV_PARAM_TYPE_INT32: ">i",
    }
    if parameter_type == mavutil.mavlink.MAV_PARAM_TYPE_REAL32:
        return float(raw_value)
    value_format = formats.get(parameter_type)
    if value_format is None:
        raise RepairError(f"unsupported MAVLink parameter type: {parameter_type}")
    return struct.unpack(value_format, packed)[0]


def _encode_parameter_value(value: int, parameter_type: int) -> float:
    """Encode an integer as the PARAM_SET float bit pattern expected by PX4."""
    formats = {
        mavutil.mavlink.MAV_PARAM_TYPE_UINT8: ">xxxB",
        mavutil.mavlink.MAV_PARAM_TYPE_INT8: ">xxxb",
        mavutil.mavlink.MAV_PARAM_TYPE_UINT16: ">xxH",
        mavutil.mavlink.MAV_PARAM_TYPE_INT16: ">xxh",
        mavutil.mavlink.MAV_PARAM_TYPE_UINT32: ">I",
        mavutil.mavlink.MAV_PARAM_TYPE_INT32: ">i",
    }
    if parameter_type == mavutil.mavlink.MAV_PARAM_TYPE_REAL32:
        return float(value)
    value_format = formats.get(parameter_type)
    if value_format is None:
        raise RepairError(f"unsupported MAVLink parameter type: {parameter_type}")
    packed = struct.pack(value_format, int(value))
    return struct.unpack(">f", packed)[0]


def _read_parameter(connection: Any, name: str, timeout_s: float = 5.0) -> tuple[int, int]:
    connection.mav.param_request_read_send(
        connection.target_system,
        connection.target_component,
        name.encode("ascii"),
        -1,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        message = connection.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
        if message is not None and _parameter_id(message) == name:
            parameter_type = int(message.param_type)
            value = _decode_parameter_value(message.param_value, parameter_type)
            return round(value), parameter_type
    raise RepairError(f"no PARAM_VALUE response for {name}")


def _set_parameter(
    connection: Any,
    name: str,
    value: int,
    parameter_type: int,
    timeout_s: float = 6.0,
) -> int:
    connection.mav.param_set_send(
        connection.target_system,
        connection.target_component,
        name.encode("ascii"),
        _encode_parameter_value(value, parameter_type),
        parameter_type,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        message = connection.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
        if message is None or _parameter_id(message) != name:
            continue
        observed = round(_decode_parameter_value(message.param_value, int(message.param_type)))
        if observed == value:
            return observed
    raise RepairError(f"PX4 did not confirm {name}={value}")


def _probe_gr01(host: str, port: int, timeout_s: float = 5.0) -> dict[str, Any]:
    connection = mavutil.mavlink_connection(
        f"tcp:{host}:{port}",
        autoreconnect=False,
        source_system=252,
        source_component=191,
    )
    message_counts: dict[str, int] = {}
    heartbeat: dict[str, Any] | None = None
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            message = connection.recv_match(blocking=True, timeout=0.5)
            if message is None:
                continue
            message_type = message.get_type()
            message_counts[message_type] = message_counts.get(message_type, 0) + 1
            if message_type == "HEARTBEAT" and message.get_srcSystem() == 1:
                heartbeat = {
                    "system_id": message.get_srcSystem(),
                    "component_id": message.get_srcComponent(),
                    "autopilot": int(message.autopilot),
                    "vehicle_type": int(message.type),
                    "system_status": int(message.system_status),
                }
                break
    finally:
        connection.close()
    return {
        "host": host,
        "port": port,
        "message_counts": message_counts,
        "heartbeat": heartbeat,
        "valid": heartbeat is not None,
    }


def _inspect_port_map(connection: Any, timeout_s: float = 15.0) -> dict[str, Any]:
    connection.mav.param_request_list_send(
        connection.target_system,
        connection.target_component,
    )
    selected: dict[str, float | int | str] = {}
    sample_names: list[str] = []
    received_messages = 0
    indices: set[int] = set()
    expected: int | None = None
    deadline = time.monotonic() + timeout_s
    last_message = time.monotonic()
    while time.monotonic() < deadline:
        if indices and time.monotonic() - last_message > 2.0:
            break
        message = connection.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.25)
        if message is None:
            continue
        if message.get_srcSystem() != connection.target_system:
            continue
        received_messages += 1
        indices.add(int(message.param_index))
        last_message = time.monotonic()
        expected = int(message.param_count)
        name = _parameter_id(message)
        if len(sample_names) < 20:
            sample_names.append(name)
        if (name.startswith("SER_") and name.endswith("_BAUD")) or name.startswith("MAV_"):
            try:
                selected[name] = _decode_parameter_value(
                    message.param_value,
                    int(message.param_type),
                )
            except RepairError:
                selected[name] = repr(message.param_value)
        if expected is not None and len(indices) >= expected:
            break
    return {
        "request_messages_sent": 1,
        "parameter_messages_received": received_messages,
        "parameters_received": len(indices),
        "parameters_expected": expected,
        "sample_names": sample_names,
        "selected": dict(sorted(selected.items())),
    }


def _reboot_disarmed_autopilot(connection: Any, timeout_s: float = 20.0) -> None:
    while connection.recv_match(blocking=False) is not None:
        pass
    connection.mav.command_long_send(
        connection.target_system,
        connection.target_component,
        mavutil.mavlink.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN,
        0,
        1,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    time.sleep(2.0)
    while connection.recv_match(blocking=False) is not None:
        pass
    heartbeat = connection.wait_heartbeat(timeout=timeout_s)
    if heartbeat is None:
        raise RepairError("V6X heartbeat did not return after the disarmed autopilot reboot")
    if heartbeat.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
        raise RepairError("V6X unexpectedly returned armed after reboot")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Repair only the V6X TELEM1 baud and verify the GR01 read-only bridge."
    )
    parser.add_argument("--endpoint", default="udp:0.0.0.0:14550")
    parser.add_argument("--gr01-host", default="192.168.144.11")
    parser.add_argument("--gr01-port", type=int, default=5760)
    parser.add_argument("--apply-baud", type=int, choices=SUPPORTED_BAUDS)
    parser.add_argument("--inspect-port-map", action="store_true")
    parser.add_argument("--acknowledge-telemetry-only-change", action="store_true")
    parser.add_argument("--reboot-v6x", action="store_true")
    parser.add_argument("--acknowledge-disarmed-v6x-reboot", action="store_true")
    args = parser.parse_args()

    if args.apply_baud is not None and not args.acknowledge_telemetry_only_change:
        raise RepairError("the telemetry-only change acknowledgement is required")
    if args.reboot_v6x and args.apply_baud is None:
        raise RepairError("--reboot-v6x requires --apply-baud")
    if args.reboot_v6x and not args.acknowledge_disarmed_v6x_reboot:
        raise RepairError("the disarmed V6X reboot acknowledgement is required")

    connection = mavutil.mavlink_connection(
        args.endpoint,
        source_system=250,
        source_component=191,
        autoreconnect=False,
    )
    writes = 0
    result: dict[str, Any] = {
        "event": "gr01_v6x_baud_repair",
        "parameter": PARAMETER_NAME,
        "requested_baud": args.apply_baud,
        "flight_commands_sent": 0,
        "actuator_commands_sent": 0,
        "payload_commands_sent": 0,
        "autopilot_reboot_commands_sent": 0,
    }
    try:
        heartbeat = connection.wait_heartbeat(timeout=8)
        if heartbeat is None:
            raise RepairError("no PX4 heartbeat arrived over the board-internal Ethernet path")
        connection.target_system = heartbeat.get_srcSystem()
        connection.target_component = heartbeat.get_srcComponent()
        armed = bool(heartbeat.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
        identity = {
            "system_id": heartbeat.get_srcSystem(),
            "component_id": heartbeat.get_srcComponent(),
            "autopilot": int(heartbeat.autopilot),
            "vehicle_type": int(heartbeat.type),
            "system_status": int(heartbeat.system_status),
            "armed": armed,
        }
        result["identity"] = identity
        if identity["system_id"] != 1 or identity["autopilot"] != mavutil.mavlink.MAV_AUTOPILOT_PX4:
            raise RepairError(f"unexpected autopilot identity: {identity}")
        if armed:
            raise RepairError("refusing a telemetry configuration change while the V6X is armed")

        if args.inspect_port_map:
            result["port_map"] = _inspect_port_map(connection)
            result["changed"] = False
            result["parameter_writes_sent"] = 0
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0

        original_baud, parameter_type = _read_parameter(connection, PARAMETER_NAME)
        result["original_baud"] = original_baud
        result["parameter_type"] = parameter_type
        if original_baud not in SUPPORTED_BAUDS:
            raise RepairError(f"unexpected {PARAMETER_NAME} value: {original_baud}")

        result["before"] = _probe_gr01(args.gr01_host, args.gr01_port)
        if args.apply_baud is None or args.apply_baud == original_baud:
            result["changed"] = False
            result["kept_baud"] = original_baud
            result["parameter_messages_sent"] = 1
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0 if result["before"]["valid"] else 1

        _set_parameter(connection, PARAMETER_NAME, args.apply_baud, parameter_type)
        writes += 1
        if args.reboot_v6x:
            _reboot_disarmed_autopilot(connection)
            result["autopilot_reboot_commands_sent"] += 1
        else:
            time.sleep(2.0)
        result["after"] = _probe_gr01(args.gr01_host, args.gr01_port)
        if result["after"]["valid"]:
            result["changed"] = True
            result["kept_baud"] = args.apply_baud
            result["rolled_back"] = False
            result["parameter_messages_sent"] = 2
            result["parameter_writes_sent"] = writes
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0

        _set_parameter(connection, PARAMETER_NAME, original_baud, parameter_type)
        writes += 1
        if args.reboot_v6x:
            _reboot_disarmed_autopilot(connection)
            result["autopilot_reboot_commands_sent"] += 1
        else:
            time.sleep(1.0)
        restored_baud, _ = _read_parameter(connection, PARAMETER_NAME)
        result["changed"] = False
        result["kept_baud"] = restored_baud
        result["rolled_back"] = True
        result["parameter_messages_sent"] = 4
        result["parameter_writes_sent"] = writes
        result["failure"] = "GR01 produced no valid MAVLink heartbeat at the requested baud"
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())

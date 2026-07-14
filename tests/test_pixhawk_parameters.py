from __future__ import annotations

import json
import socket
import struct
import threading
from pathlib import Path

import pytest

from multidetect.pixhawk_parameters import (
    PixhawkParameterBackupClient,
    PixhawkParameterBackupConfig,
    compare_pixhawk_parameter_snapshots,
    load_verified_pixhawk_parameter_snapshot,
    write_pixhawk_parameter_snapshot,
)


class _Message:
    def __init__(
        self,
        *,
        source_system_id: int = 1,
        source_component_id: int = 1,
        param_id: bytes = b"TEST_PARAM",
        param_value: float = 1.0,
        param_type: int = 9,
        param_count: int = 1,
        param_index: int = 0,
    ) -> None:
        self._source_system_id = source_system_id
        self._source_component_id = source_component_id
        self.param_id = param_id
        self.param_value = param_value
        self.param_type = param_type
        self.param_count = param_count
        self.param_index = param_index

    def get_srcSystem(self) -> int:
        return self._source_system_id

    def get_srcComponent(self) -> int:
        return self._source_component_id


class _RecordingMav:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def param_request_list_send(self, *args: object) -> None:
        self.calls.append(("param_request_list_send", args))


class _Connection:
    def __init__(self, messages: list[_Message]) -> None:
        self.mav = _RecordingMav()
        self.messages = list(messages)

    def recv_match(self, *, type: str, blocking: bool, timeout: float):
        assert type == "PARAM_VALUE"
        assert blocking is True
        assert timeout > 0
        return self.messages.pop(0) if self.messages else None


@pytest.mark.parametrize("hash_index", [32_767, 65_535])
def test_parameter_backup_sends_one_read_request_and_no_write_or_command(
    hash_index: int,
) -> None:
    connection = _Connection(
        [
            _Message(
                param_id=b"_HASH_CHECK",
                param_value=123.0,
                param_count=2,
                param_index=hash_index,
            ),
            _Message(source_system_id=2, param_count=2),
            _Message(param_id=b"SYS_AUTOSTART", param_value=2100.0, param_count=2),
            _Message(
                param_id=b"MAV_SYS_ID",
                param_value=1.0,
                param_count=2,
                param_index=1,
            ),
        ]
    )
    client = PixhawkParameterBackupClient(
        PixhawkParameterBackupConfig(
            "udpout:127.0.0.1:14550",
            parameter_encoding="c_cast",
            active_read_request_acknowledged=True,
            minimum_parameters=2,
        ),
        connection=connection,
    )

    snapshot = client.capture()
    document = snapshot.to_document()

    assert connection.mav.calls == [("param_request_list_send", (1, 1))]
    assert snapshot.complete is True
    assert snapshot.passed is True
    assert snapshot.rejected_source_message_count == 1
    assert snapshot.px4_parameter_hash_raw_hex is not None
    assert [parameter.name for parameter in snapshot.parameters] == [
        "SYS_AUTOSTART",
        "MAV_SYS_ID",
    ]
    assert document["active_read_requests_transmitted"] == 1
    assert document["messages_transmitted"] == 1
    assert document["parameter_write_messages_transmitted"] == 0
    assert document["flight_command_messages_transmitted"] == 0
    assert document["mission_messages_transmitted"] == 0
    assert document["actuator_messages_transmitted"] == 0
    assert len(document["parameter_list_sha256"]) == 64
    assert snapshot.parameters[0].value == 2100
    assert client.parameter_write_messages_transmitted == 0
    assert client.hardware_control_enabled is False

    with pytest.raises(RuntimeError, match="single-use"):
        client.capture()


def test_parameter_backup_reads_bytewise_value_from_mavlink2_wire_payload() -> None:
    raw_value = struct.pack("<i", 10)
    message = _Message(
        param_id=b"COM_DL_LOSS_T",
        param_value=struct.unpack("<f", raw_value)[0],
        param_type=6,
    )
    payload = raw_value + struct.pack("<HH", 1, 0) + b"COM_DL_LOSS_T\0\0\0" + b"\x06"
    header = b"\xfd" + bytes([len(payload), 0, 0, 1, 1, 1, 22, 0, 0])
    message.get_payload = lambda: b"\x01\x16\x00\x00" + payload  # type: ignore[attr-defined]
    message.get_msgbuf = lambda: header + payload + b"\x00\x00"  # type: ignore[attr-defined]
    client = PixhawkParameterBackupClient(
        PixhawkParameterBackupConfig(
            "udpout:127.0.0.1:14550",
            parameter_encoding="bytewise",
            active_read_request_acknowledged=True,
        ),
        connection=_Connection([message]),
    )

    snapshot = client.capture()

    assert snapshot.complete is True
    assert snapshot.parameters[0].value == 10
    assert snapshot.parameters[0].raw_value_hex == "0a000000"


def test_parameter_backup_writes_partial_snapshot_but_fails_closed() -> None:
    connection = _Connection(
        [
            _Message(
                param_id=b"ONLY_ONE",
                param_count=2,
                param_index=0,
            )
        ]
    )
    client = PixhawkParameterBackupClient(
        PixhawkParameterBackupConfig(
            "udpout:127.0.0.1:14550",
            parameter_encoding="c_cast",
            active_read_request_acknowledged=True,
            timeout_seconds=0.02,
            idle_timeout_seconds=0.005,
            minimum_parameters=2,
        ),
        connection=connection,
    )

    snapshot = client.capture()

    assert snapshot.complete is False
    assert snapshot.passed is False
    assert snapshot.received_parameter_count == 1
    assert any("incomplete" in reason for reason in snapshot.failure_reasons)
    assert snapshot.active_read_requests_transmitted == 1


def test_parameter_snapshot_is_atomic_and_rejects_overwrite(tmp_path: Path) -> None:
    client = PixhawkParameterBackupClient(
        PixhawkParameterBackupConfig(
            "udpout:127.0.0.1:14550",
            parameter_encoding="c_cast",
            active_read_request_acknowledged=True,
            minimum_parameters=1,
        ),
        connection=_Connection([_Message()]),
    )
    snapshot = client.capture()
    output = tmp_path / "parameters.json"

    write_pixhawk_parameter_snapshot(output, snapshot)

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["complete"] is True
    assert document["parameter_write_messages_transmitted"] == 0
    assert list(tmp_path.glob("*.tmp")) == []
    with pytest.raises(FileExistsError, match="already exists"):
        write_pixhawk_parameter_snapshot(output, snapshot)
    write_pixhawk_parameter_snapshot(output, snapshot, force=True)


def test_real_pymavlink_udp_parameter_backup_hil() -> None:
    mavutil = pytest.importorskip("pymavlink.mavutil")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = mavutil.mavlink_connection(
        f"udpin:127.0.0.1:{port}",
        source_system=1,
        source_component=1,
    )
    observed_request = []

    def respond() -> None:
        request = server.recv_match(
            type="PARAM_REQUEST_LIST",
            blocking=True,
            timeout=3.0,
        )
        observed_request.append(request)
        if request is None:
            return
        server.mav.param_value_send(
            b"SYS_AUTOSTART",
            struct.unpack("<f", struct.pack("<i", 2100))[0],
            mavutil.mavlink.MAV_PARAM_TYPE_INT32,
            3,
            0,
        )
        server.mav.param_value_send(
            b"MAV_SYS_ID",
            struct.unpack("<f", struct.pack("<i", 1))[0],
            mavutil.mavlink.MAV_PARAM_TYPE_INT32,
            3,
            1,
        )
        server.mav.param_value_send(
            b"COM_ARM_WO_GPS",
            struct.unpack("<f", struct.pack("<i", 0))[0],
            mavutil.mavlink.MAV_PARAM_TYPE_INT32,
            3,
            2,
        )

    thread = threading.Thread(target=respond)
    thread.start()
    client = PixhawkParameterBackupClient(
        PixhawkParameterBackupConfig(
            endpoint=f"udpout:127.0.0.1:{port}",
            parameter_encoding="bytewise",
            active_read_request_acknowledged=True,
            timeout_seconds=3.0,
            idle_timeout_seconds=0.5,
            minimum_parameters=3,
        )
    )
    try:
        snapshot = client.capture()
    finally:
        client.close()
        thread.join(timeout=3.0)
        server.close()

    assert thread.is_alive() is False
    assert observed_request[0].target_system == 1
    assert observed_request[0].target_component == 1
    assert snapshot.complete is True
    assert snapshot.passed is True
    assert snapshot.received_parameter_count == 3
    assert [parameter.value for parameter in snapshot.parameters] == [2100, 1, 0]
    assert snapshot.active_read_requests_transmitted == 1
    assert client.parameter_write_messages_transmitted == 0


def test_verified_snapshot_loader_detects_parameter_tampering(tmp_path: Path) -> None:
    snapshot = _snapshot_for_values({"PARAM_A": 1.0, "PARAM_B": 2.0})
    output = tmp_path / "parameters.json"
    write_pixhawk_parameter_snapshot(output, snapshot)

    verified = load_verified_pixhawk_parameter_snapshot(output)

    assert verified.parameter_list_sha256 == snapshot.parameter_list_sha256
    document = json.loads(output.read_text(encoding="utf-8"))
    document["parameters"][0]["value"] = 99.0
    output.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="value mismatch"):
        load_verified_pixhawk_parameter_snapshot(output)

    write_pixhawk_parameter_snapshot(output, snapshot, force=True)
    document = json.loads(output.read_text(encoding="utf-8"))
    document["messages_transmitted"] = True
    output.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="invariant failed"):
        load_verified_pixhawk_parameter_snapshot(output)


def test_parameter_diff_rejects_unlisted_and_requires_expected_changes() -> None:
    before = _snapshot_for_values({"PARAM_A": 1.0, "PARAM_B": 2.0})
    after = _snapshot_for_values({"PARAM_A": 1.0, "PARAM_B": 3.0})

    rejected = compare_pixhawk_parameter_snapshots(before, after)
    accepted = compare_pixhawk_parameter_snapshots(
        before,
        after,
        allowed_changes=frozenset({"PARAM_B"}),
        required_changes=frozenset({"PARAM_B"}),
    )
    missing = compare_pixhawk_parameter_snapshots(
        before,
        after,
        allowed_changes=frozenset({"PARAM_B", "PARAM_C"}),
        required_changes=frozenset({"PARAM_C"}),
    )

    assert rejected["gate_passed"] is False
    assert rejected["unexpected_change_names"] == ["PARAM_B"]
    assert accepted["gate_passed"] is True
    assert accepted["observed_change_names"] == ["PARAM_B"]
    assert accepted["messages_transmitted"] == 0
    assert missing["gate_passed"] is False
    assert missing["missing_required_change_names"] == ["PARAM_C"]


@pytest.mark.parametrize(
    "changes",
    [
        {"parameter_encoding": "unknown"},
        {"active_read_request_acknowledged": False},
        {"target_system_id": 0},
        {"target_component_id": 256},
        {"local_system_id": True},
        {"timeout_seconds": float("nan")},
        {"idle_timeout_seconds": 31.0},
        {"minimum_parameters": 0},
        {"minimum_parameters": 2, "maximum_parameters": 1},
    ],
)
def test_parameter_backup_config_rejects_unsafe_values(changes: dict[str, object]) -> None:
    options: dict[str, object] = {
        "parameter_encoding": "bytewise",
        "active_read_request_acknowledged": True,
        **changes,
    }
    with pytest.raises(ValueError, match="Pixhawk parameter"):
        PixhawkParameterBackupConfig(
            "udpout:127.0.0.1:14550",
            **options,  # type: ignore[arg-type]
        )


def _snapshot_for_values(values: dict[str, float]):
    messages = [
        _Message(
            param_id=name.encode("ascii"),
            param_value=value,
            param_type=9,
            param_count=len(values),
            param_index=index,
        )
        for index, (name, value) in enumerate(values.items())
    ]
    return PixhawkParameterBackupClient(
        PixhawkParameterBackupConfig(
            "udpout:127.0.0.1:14550",
            parameter_encoding="c_cast",
            active_read_request_acknowledged=True,
            minimum_parameters=len(values),
        ),
        connection=_Connection(messages),
    ).capture()

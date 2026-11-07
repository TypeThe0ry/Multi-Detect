"""Bounded CPU data/lifecycle fixtures; no actual vehicle, socket or authority."""

from __future__ import annotations

import array
import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_onboard_teacher_telemetry_capture import Message

from scripts import onboard_teacher_observed_context as observed
from scripts import onboard_teacher_telemetry_capture_v2 as v2

ROOT = Path(__file__).resolve().parents[1]


def make_capture(tmp_path, messages=(), **options):
    queue, calls = list(messages), []
    current = [10.0]

    def clock():
        current[0] += 0.001
        return current[0]

    def reader(timeout):
        calls.append(timeout)
        return queue.pop(0) if queue else None

    capture = v2.TeacherTelemetryCaptureV2(
        tmp_path / "raw",
        reader=options.pop("reader", reader),
        clock=options.pop("clock", clock),
        clock_id="original-QPC-domain",
        transaction_id="owned-transaction",
        local_origin_id="unresolved-origin",
        **options,
    )
    return capture, calls


def assert_same_type_bits(original, restored):
    assert type(original) is type(restored)
    if type(original) is float:
        assert struct.pack(">d", original) == struct.pack(">d", restored)
    elif type(original) is array.array:
        assert (original.typecode, original.itemsize, original.tobytes()) == (
            restored.typecode,
            restored.itemsize,
            restored.tobytes(),
        )
    elif type(original) in (list, tuple):
        assert len(original) == len(restored)
        for first, second in zip(original, restored, strict=True):
            assert_same_type_bits(first, second)
    elif type(original) is dict:
        assert list(original) == list(restored)
        for key in original:
            assert_same_type_bits(original[key], restored[key])
    else:
        assert original == restored


@pytest.mark.parametrize(
    "value",
    [
        None,
        False,
        True,
        0,
        -123,
        2**64 - 1,
        "UTF8-汉字",
        "",
        b"",
        b"\x00\xff",
        bytearray(b"\x00\xff"),
        0.0,
        -0.0,
        1.23456789,
        float("inf"),
        float("-inf"),
        struct.unpack(">d", bytes.fromhex("7ff8000000000123"))[0],
        struct.unpack(">d", bytes.fromhex("fff8000000000456"))[0],
        [],
        (),
        {"type": "bytes", "value": "ffff"},
        {v2.TAG: ["bytes", "ff"]},
        ["bytes", "ff"],
        (1, bytearray(b"x"), (2, 3)),
        array.array("H", [16000, 65535]),
        array.array("f", [0.25, float("nan"), float("inf")]),
        array.array("u", "汉字"),
    ],
)
def test_lossless_codec_distinguishes_types_and_float_bits(value):
    encoded = v2.encode_typed_value(value)
    restored = v2.decode_typed_value(json.loads(json.dumps(encoded, allow_nan=False)))
    assert_same_type_bits(value, restored)


def test_native_fields_unmodified_numeric_sequences_compatible_binary_tagged(tmp_path):
    message = Message(
        kind="BATTERY_STATUS",
        time_remaining=1200,
        voltages=array.array("H", [16000, 65535]),
        tuple_value=(1, 2),
        data=bytearray(b"\x00\xff"),
    )
    cap, calls = make_capture(tmp_path, [message])
    original, row = cap.receive(1)
    assert original is message and len(calls) == 1
    assert row["message"]["time_remaining"] == 1200
    assert row["message"]["voltages"] == [16000, 65535]
    assert row["message"]["tuple_value"] == [1, 2]
    assert row["message"]["data"] == {v2.TAG: ["bytearray", "00ff"]}
    receipt = cap.seal()
    typed = json.loads(Path(receipt["records"][0]["typed_projection"]["path"]).read_bytes())
    assert typed["packet_sha256"] == row["packet_sha256"]
    assert_same_type_bits(message.fields, v2.decode_typed_value(typed["value"]))
    assert receipt["contract"] == v2.CONTRACT
    assert receipt["encoding"] == v2.ENCODING
    assert receipt["orphan_packet_count"] == 0
    assert not any(receipt["claims"].values())


def test_reserved_dictionary_marker_is_escaped_not_impersonated(tmp_path):
    source = {v2.TAG: ["bytes", "ffff"], "plain": 12}
    cap, _ = make_capture(tmp_path, [Message(extra=source)])
    _, row = cap.receive(1)
    escaped = row["message"]["extra"]
    assert set(escaped) == {v2.TAG}
    assert_same_type_bits(source, v2.decode_typed_value(escaped[v2.TAG]))
    assert escaped != source
    cap.seal()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_stays_bits_and_cannot_qualify_numeric_context(tmp_path, value):
    cap, _ = make_capture(tmp_path, [Message(x=value)])
    _, row = cap.receive(1)
    encoded = row["message"]["x"][v2.TAG]
    assert struct.pack(">d", v2.decode_typed_value(encoded)) == struct.pack(">d", value)
    with pytest.raises(ValueError):
        observed._number(row["message"], "x")
    assert row["message"]["x"] != 0
    cap.seal()


def test_original_packet_fsync_precedes_all_decoded_access(tmp_path):
    capture = None

    class OrderedMessage(Message):
        def assert_raw_first(self):
            path = capture.directory / "receive-00000.bin"
            assert path.read_bytes() == self.payload
            assert capture._attempts[0]["packet_durable"] is True
            assert (capture.directory / "receive-00000-pending.json").is_file()

        def get_type(self):
            self.assert_raw_first()
            return super().get_type()

        def get_srcSystem(self):
            self.assert_raw_first()
            return super().get_srcSystem()

        def to_dict(self):
            self.assert_raw_first()
            return super().to_dict()

    capture, _ = make_capture(tmp_path, [OrderedMessage()])
    capture.receive(1)
    capture.seal()


@pytest.mark.parametrize(
    "stage", ["get_type", "get_srcSystem", "get_srcComponent", "get_seq", "to_dict"]
)
def test_decoded_accessor_exception_retains_original_packet_and_poison(tmp_path, stage):
    message = Message()

    def fail():
        raise MemoryError("decoded accessor")

    setattr(message, stage, fail)
    cap, _ = make_capture(tmp_path, [message, Message()])
    with pytest.raises(MemoryError, match="decoded accessor"):
        cap.receive(1)
    assert (cap.directory / "receive-00000.bin").read_bytes() == message.payload
    assert cap._latest == {} and cap._records == []
    with pytest.raises(RuntimeError):
        cap.receive(1)
    with pytest.raises(RuntimeError):
        cap.seal()
    receipt = cap.abort("unknown accessor failure")
    assert receipt["packet_attempt_count"] == receipt["orphan_packet_count"] == 1
    assert receipt["packet_attempts"][0]["packet_durable"] is True
    assert receipt["record_count"] == 0


@pytest.mark.parametrize("filename", [".bin", "-pending.json", "-typed.json", ".json"])
def test_partial_fsync_failure_retains_original_and_prevents_cache(tmp_path, monkeypatch, filename):
    cap, _ = make_capture(tmp_path, [Message()])
    real_write = v2._write_new
    suffix = "receive-00000" + filename

    def fail_after_file(path, raw):
        if path.name == suffix:
            with path.open("xb") as stream:
                stream.write(raw)
                stream.flush()
            raise OSError("fsync failure")
        return real_write(path, raw)

    monkeypatch.setattr(v2, "_write_new", fail_after_file)
    with pytest.raises(OSError, match="fsync failure"):
        cap.receive(1)
    assert (cap.directory / "receive-00000.bin").read_bytes() == Message().payload
    assert cap._latest == {} and cap._records == []
    receipt = cap.abort("fsync failure")
    assert receipt["record_count"] == 0 and receipt["orphan_packet_count"] == 1
    attempt = receipt["packet_attempts"][0]
    assert attempt["packet_durable"] is (filename != ".bin")
    assert attempt["status"] == ("packet-write-failed" if filename == ".bin" else "orphan-packet")


def test_late_observer_failure_has_packet_sidecar_envelope_but_no_cache(tmp_path):
    # receive clock: deadline, remaining, received, then dispatch-after-fsync.
    times = iter([10, 10, 10, 9])
    cap, _ = make_capture(tmp_path, [Message()], clock=lambda: next(times))
    with pytest.raises(ValueError, match="regressed"):
        cap.receive(1)
    assert cap._records == [] and cap._latest == {}
    receipt = cap.abort("late clock")
    attempt = receipt["packet_attempts"][0]
    for name in ("packet", "pending", "typed_projection", "envelope"):
        assert Path(attempt[name]["path"]).is_file()
    assert receipt["orphan_packet_count"] == 1


@pytest.mark.parametrize(
    "variant", ["bad_fields", "type", "bool_source", "metadata_collision", "unsupported"]
)
def test_invalid_projection_keeps_original_packet_not_accepted_cache(tmp_path, variant):
    message = Message()
    if variant == "bad_fields":
        message.fields["mavpackettype"] = "HEARTBEAT"
    elif variant == "type":
        message.kind = "invalid_type"
    elif variant == "bool_source":
        message.system = True
    elif variant == "metadata_collision":
        message.fields["system_id"] = 2
    else:
        message.fields["opaque"] = object()
    cap, _ = make_capture(tmp_path, [message])
    with pytest.raises((TypeError, ValueError)):
        cap.receive(1)
    receipt = cap.abort("unsupported projection")
    assert receipt["record_count"] == 0 and receipt["orphan_packet_count"] == 1
    packet = receipt["packet_attempts"][0]["packet"]
    assert Path(packet["path"]).read_bytes() == message.payload
    assert packet["sha256"] == hashlib.sha256(message.payload).hexdigest()


@pytest.mark.parametrize(
    "value",
    [
        object(),
        {1: "nonstring"},
        "x" * 16385,
        b"x" * 16385,
        1 << 4096,
        list(range(2049)),
        array.array("B", [0] * 2049),
    ],
)
def test_codec_rejects_unsupported_or_oversize(value):
    with pytest.raises((ValueError, TypeError)):
        v2.encode_typed_value(value)


def test_codec_rejects_cycles_depth_and_combined_node_limit():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="cyclic"):
        v2.encode_typed_value(cycle)
    nested = 1
    for _ in range(17):
        nested = [nested]
    with pytest.raises(ValueError, match="depth"):
        v2.encode_typed_value(nested)
    with pytest.raises(ValueError, match="node"):
        v2.encode_typed_value([list(range(1024)), list(range(1024))])


@pytest.mark.parametrize(
    "node",
    [
        ["unknown", 0],
        ["bool", 1],
        ["int", "01"],
        ["int", "-0"],
        ["float64", "ff"],
        ["bytes", "FF"],
        ["bytes", "aa bb"],
        ["dict", [["x", ["none"]], ["x", ["none"]]]],
        ["array", "unknown", 1, sys.byteorder, "00"],
        ["array", "H", True, sys.byteorder, "0000"],
        ["array", "H", 2, "big" if sys.byteorder == "little" else "little", "0000"],
        ["array", "H", 2, sys.byteorder, "00"],
    ],
)
def test_malformed_typed_nodes_reject(node):
    with pytest.raises((ValueError, TypeError)):
        v2.decode_typed_value(node)


def test_foreign_packets_order_none_and_capacity_are_inherited(tmp_path):
    cap, calls = make_capture(tmp_path, [Message(system=2), Message()], max_records=2)
    _, row = cap.receive(1)
    assert row["record_index"] == 1 and len(calls) == 2
    assert cap.receive(0) is None
    receipt = cap.seal()
    assert receipt["accepted_source_count"] == receipt["foreign_source_count"] == 1
    assert receipt["record_count"] == receipt["packet_attempt_count"] == 2
    assert [r["status"] for r in receipt["packet_attempts"]] == ["projection-complete"] * 2


def test_byte_budget_counts_raw_pending_sidecar_envelope_exactly(tmp_path):
    cap, _ = make_capture(tmp_path, [Message()])
    cap.receive(1)
    data_files = list(cap.directory.iterdir())
    expected = sum(path.stat().st_size for path in data_files)
    receipt = cap.seal()
    assert receipt["payload_bytes"] == expected
    assert len(data_files) == 4
    for path in data_files:
        assert path.name != "capture-receipt.json"


def test_total_budget_failure_never_deletes_previously_durable_packet(tmp_path, monkeypatch):
    message = Message()
    cap, _ = make_capture(tmp_path, [message])
    monkeypatch.setattr(v2, "MAX_TOTAL_BYTES", len(message.payload))
    with pytest.raises(ValueError, match="total byte budget"):
        cap.receive(1)
    receipt = cap.abort("budget exhausted")
    assert receipt["payload_bytes"] == len(message.payload)
    assert receipt["record_count"] == 0 and receipt["orphan_packet_count"] == 1
    assert (cap.directory / "receive-00000.bin").read_bytes() == message.payload


def test_create_only_collision_retains_existing_user_file(tmp_path):
    cap, _ = make_capture(tmp_path, [Message()])
    path = cap.directory / "receive-00000.bin"
    path.write_bytes(b"existing-user")
    with pytest.raises(FileExistsError):
        cap.receive(1)
    assert path.read_bytes() == b"existing-user"
    receipt = cap.abort("create-only")
    assert receipt["packet_attempts"][0]["packet_durable"] is False


def test_capacity_poison_no_hidden_budget_reset(tmp_path):
    cap, _ = make_capture(tmp_path, [Message(), Message()], max_records=1)
    cap.receive(1)
    with pytest.raises(ValueError, match="capacity"):
        cap.receive(1)
    receipt = cap.abort("capacity")
    assert receipt["record_count"] == receipt["packet_attempt_count"] == 1
    assert receipt["status"] == "aborted"


def test_detached_latest_same_time_domain_and_no_double_close(tmp_path):
    message = Message()
    cap, _ = make_capture(tmp_path, [message])
    _, row = cap.receive(1)
    row["message"]["x"] = message.fields["x"] = 999
    latest = cap.latest_message("LOCAL_POSITION_NED", now_s=10.1, maximum_age_s=1)
    assert latest["message"]["x"] == 0
    latest["message"]["x"] = 100
    assert cap.latest_message("LOCAL_POSITION_NED", now_s=9, maximum_age_s=1) is None
    assert cap.latest_message("LOCAL_POSITION_NED", now_s=20, maximum_age_s=1) is None
    assert (
        cap.latest_message("LOCAL_POSITION_NED", now_s=10.1, maximum_age_s=1)["message"]["x"] == 0
    )
    cap.seal()
    with pytest.raises(RuntimeError):
        cap.receive(1)
    with pytest.raises(RuntimeError):
        cap.abort("closed")


def test_wrong_thread_and_reentrant_receiver_still_poison(tmp_path):
    cap, _ = make_capture(tmp_path)
    errors = []

    def foreign():
        try:
            cap.receive(1)
        except RuntimeError as error:
            errors.append(error)

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(2)
    assert len(errors) == 1 and cap.abort("owner")["status"] == "aborted"
    (tmp_path / "other").mkdir()
    recursive = None
    recursive, _ = make_capture(tmp_path / "other", reader=lambda _: recursive.receive(0))
    with pytest.raises(RuntimeError, match="Reentrant"):
        recursive.receive(1)
    recursive.abort("recursive")


def test_all_nine_native_rows_pass_frozen_observed_bridge(tmp_path):
    # Reuse the sealed bridge's native field fixture; only NEW V2 writes here.
    from test_onboard_teacher_observed_context import fixture as native_fixture

    fixture = native_fixture.__wrapped__(tmp_path)
    messages = []
    for kind in observed.REQUIRED_MESSAGES:
        old = fixture["rows"][kind]["message"]
        fields = {
            k: v
            for k, v in old.items()
            if k
            not in (
                "message_type",
                "source_system_id",
                "source_component_id",
                "mavpackettype",
            )
        }
        message = Message(kind=kind)
        message.fields = {"mavpackettype": kind, **fields}
        messages.append(message)
    (tmp_path / "v2").mkdir()
    cap, _ = make_capture(tmp_path / "v2", messages)
    cap.clock_id, cap.transaction_id, cap.local_origin_id = (
        fixture["clock_id"],
        fixture["transaction_id"],
        fixture["local_origin_id"],
    )
    fixture["rows"] = {kind: cap.receive(1)[1] for kind in observed.REQUIRED_MESSAGES}
    cap.seal()
    result = observed.build_observed_teacher_context(**fixture)
    assert result.context.aircraft_position.latitude_deg == pytest.approx(1.3)
    assert not result.to_mapping()["claims"]["hardware_control_enabled"]


@pytest.fixture(scope="module")
def installed_dialect():
    pymavlink_spec = importlib.util.find_spec("pymavlink")
    if pymavlink_spec is None or not pymavlink_spec.submodule_search_locations:
        pytest.skip("optional dependency pymavlink is not installed")
    primary = (
        Path(next(iter(pymavlink_spec.submodule_search_locations)))
        / "dialects/v20/ardupilotmega.py"
    )
    if not primary.is_file():
        pytest.skip("pymavlink ardupilotmega dialect is not installed")
    spec = importlib.util.spec_from_file_location("teacher_primary_dialect_fixture", primary)
    dialect = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dialect)
    return dialect


def test_real_installed_bad_data_class_is_foreign_binary_not_qualification(
    tmp_path, installed_dialect
):
    dialect = installed_dialect
    original = dialect.MAVLink_bad_data(bytearray(b"\x00\xffBAD"), "fixture undecodable")
    assert type(original.to_dict()["data"]) is bytearray
    cap, _ = make_capture(tmp_path, [original])
    assert cap.receive(0) is None
    receipt = cap.seal()
    assert receipt["foreign_source_count"] == 1 and receipt["accepted_source_count"] == 0
    packet = receipt["records"][0]["packet"]
    assert Path(packet["path"]).read_bytes() == original.get_msgbuf()
    assert cap._latest == {}
    bytes_original = dialect.MAVLink_bad_data(b"\x00\xffBAD", "fixture undecodable")
    assert type(bytes_original.to_dict()["data"]) is str  # primary preexisting formatter, NOT V2


@pytest.mark.parametrize("kind", ["BATTERY_STATUS", "GPS_RAW_INT", "LOCAL_POSITION_NED"])
def test_real_primary_pack_parse_numeric_values_preserved(tmp_path, installed_dialect, kind):
    dialect = installed_dialect
    messages = {
        "BATTERY_STATUS": lambda: dialect.MAVLink_battery_status_message(
            id=0,
            battery_function=1,
            type=0,
            temperature=2000,
            voltages=array.array("H", [16000] + [65535] * 9),
            current_battery=0,
            current_consumed=0,
            energy_consumed=0,
            battery_remaining=80,
            time_remaining=1200,
            charge_state=1,
        ),
        "GPS_RAW_INT": lambda: dialect.MAVLink_gps_raw_int_message(
            time_usec=2**63 + 123,
            fix_type=3,
            lat=13000000,
            lon=1038000000,
            alt=18000,
            eph=120,
            epv=170,
            vel=0,
            cog=0,
            satellites_visible=12,
            h_acc=1000,
        ),
        "LOCAL_POSITION_NED": lambda: dialect.MAVLink_local_position_ned_message(
            time_boot_ms=12345,
            x=0,
            y=1,
            z=-12,
            vx=0,
            vy=0,
            vz=0,
        ),
    }
    constructed = messages[kind]()
    wire = constructed.pack(dialect.MAVLink(None, srcSystem=1, srcComponent=1))
    parsed = dialect.MAVLink(None).parse_char(wire)
    assert parsed.get_srcSystem() == parsed.get_srcComponent() == 1
    cap, _ = make_capture(tmp_path, [parsed])
    original, row = cap.receive(1)
    assert original is parsed and Path(row["packet"]["path"]).read_bytes() == wire
    if kind == "BATTERY_STATUS":
        assert row["message"]["voltages"] == [16000] + [65535] * 9
        assert row["message"]["time_remaining"] == 1200
    elif kind == "GPS_RAW_INT":
        assert row["message"]["time_usec"] == 2**63 + 123
        assert row["message"]["h_acc"] == 1000
    else:
        assert row["message"]["time_boot_ms"] == 12345
        assert row["message"]["z"] == -12.0
    receipt = cap.seal()
    typed = json.loads(Path(receipt["records"][0]["typed_projection"]["path"]).read_bytes())
    assert_same_type_bits(parsed.to_dict(), v2.decode_typed_value(typed["value"]))


def test_guarded_fresh_process_import_no_ml_or_socket():
    code = (
        "import sys; "
        f"sys.path[:0]=[{str(ROOT)!r},{str(ROOT / 'src')!r}]; "
        "from scripts import onboard_teacher_telemetry_capture_v2 as m; "
        "assert not any(x in sys.modules for x in "
        "('torch','transformers','pymavlink','PIL','numpy')); "
        "assert not any(m.FALSE_CLAIMS.values()); print('cpu-stdlib-only')"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "cpu-stdlib-only"

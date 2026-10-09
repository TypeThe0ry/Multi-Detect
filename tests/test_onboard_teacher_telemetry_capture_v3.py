"""CPU journal/data fixtures only; no socket, vehicle, model or authority."""

from __future__ import annotations

import array
import copy
import hashlib
import json
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_onboard_teacher_telemetry_capture import Message

from scripts import onboard_teacher_observed_context as observed
from scripts import onboard_teacher_telemetry_capture_v3 as v3


class Clock:
    def __init__(self, value=10.1):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.value += 0.0001
        return self.value


def capture(tmp_path, **kwargs):
    clock = kwargs.pop("clock", Clock())
    cap = v3.TeacherTelemetryCaptureV3(
        tmp_path / "journals",
        clock=clock,
        clock_id="same-QPC-domain",
        transaction_id="owned-transaction",
        local_origin_id="unresolved-origin",
        **kwargs,
    )
    return cap, clock


def projection_document(cap, row):
    binding = row["projection_record"]
    with Path(binding["path"]).open("rb") as stream:
        stream.seek(binding["offset"])
        raw = stream.read(binding["bytes"])
    assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    return json.loads(raw)


def assert_bits(original, restored):
    assert type(original) is type(restored)
    if type(original) is float:
        assert struct.pack(">d", original) == struct.pack(">d", restored)
    elif type(original) is array.array:
        assert (original.typecode, original.tobytes()) == (restored.typecode, restored.tobytes())
    elif type(original) in (list, tuple):
        for first, second in zip(original, restored, strict=True):
            assert_bits(first, second)
    elif type(original) is dict:
        assert list(original) == list(restored)
        for key in original:
            assert_bits(original[key], restored[key])
    else:
        assert original == restored


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        2**64 - 1,
        -0.0,
        struct.unpack(">d", bytes.fromhex("7ff8000000000123"))[0],
        float("inf"),
        float("-inf"),
        b"\x00\xff",
        bytearray(b"\x00\xff"),
        (1, 2),
        array.array("H", [16000, 65535]),
        array.array("f", [float("nan"), 0.25]),
        {v3._codec.TAG: ["bytes", "ff"]},
    ],
)
def test_typed_journal_preserves_values_bits_and_native_projection(tmp_path, value):
    cap, _ = capture(tmp_path)
    message = Message(extra=value, time_boot_ms=2**32 - 1)
    row = cap.append_batch([(message, 10.0)])[0]
    document = projection_document(cap, row)
    assert_bits(message.fields, v3._codec.decode_typed_value(document["typed_value"]))
    assert row["message"]["time_boot_ms"] == 2**32 - 1
    assert row["received_at_s"] == 10.0
    if type(value) in (tuple, array.array):
        assert type(row["message"]["extra"]) is list
    receipt = cap.seal()
    assert receipt["record_count"] == 1 and receipt["orphan_packet_count"] == 0
    assert not any(receipt["claims"].values()) and receipt["owns_socket"] is False
    assert all(stream.closed for stream in cap._streams.values())


def test_whole_raw_batch_commits_before_any_decoded_getter(tmp_path):
    cap, _ = capture(tmp_path)
    messages = []

    class Ordered(Message):
        def check(self):
            assert cap._commits["raw"]["verified"] == 1
            assert cap._commits["projection"]["attempted"] == 0
            raw = Path(cap._streams["raw"].name).read_bytes()
            assert all(msg.payload in raw for msg in messages)
            assert all(a["status"] == "raw-committed" for a in cap._attempts)

        def get_type(self):
            self.check()
            return super().get_type()

        def get_srcSystem(self):
            self.check()
            return super().get_srcSystem()

        def get_srcComponent(self):
            self.check()
            return super().get_srcComponent()

        def get_seq(self):
            self.check()
            return super().get_seq()

        def to_dict(self):
            self.check()
            return super().to_dict()

    messages = [Ordered() for _ in range(16)]
    for index, message in enumerate(messages):
        message.payload = f"actual-original-{index}".encode()
    rows = cap.append_batch([(m, 10.0 + i * 0.001) for i, m in enumerate(messages)])
    assert len(rows) == 16
    assert cap._commits["projection"]["verified"] == 1
    assert cap.seal()["record_count"] == 16


def test_two_fsyncs_per_batch_not_four_per_record(tmp_path, monkeypatch):
    cap, _ = capture(tmp_path)
    original, calls = v3.os.fsync, []

    def counted(fd):
        calls.append(fd)
        return original(fd)

    with monkeypatch.context() as change:
        change.setattr(v3.os, "fsync", counted)
        for _ in range(2):
            cap.append_batch([(Message(), 10.0)] * 16)
    assert len(calls) == 4
    receipt = cap.seal()
    assert receipt["record_count"] == 32
    assert receipt["commit_counts"] == {
        "raw": {"attempted": 2, "fsync_returned": 2, "verified": 2},
        "projection": {"attempted": 2, "fsync_returned": 2, "verified": 2},
    }
    assert all(set(b["durations_s"]) == {"raw", "projection", "total"} for b in receipt["batches"])


def test_foreign_zero_zero_preserved_but_never_latest_and_detached(tmp_path):
    cap, clock = capture(tmp_path)
    rows = cap.append_batch(
        [
            (Message(system=1, x=7.0), 10.0),
            (Message(system=0, component=0, data=bytearray(b"bad")), 10.01),
        ]
    )
    assert [r["source_accepted"] for r in rows] == [True, False]
    rows[0]["message"]["x"] = 999
    latest = cap.latest_message("LOCAL_POSITION_NED", now_s=clock.value, maximum_age_s=2)
    assert latest["message"]["x"] == 7.0 and latest["received_at_s"] == 10.0
    latest["raw_record"]["offset"] = 9999
    assert (
        cap.latest_message("LOCAL_POSITION_NED", now_s=clock.value, maximum_age_s=2)["raw_record"][
            "offset"
        ]
        != 9999
    )
    assert cap.latest_message("LOCAL_POSITION_NED", now_s=clock.value + 3, maximum_age_s=2) is None
    receipt = cap.seal()
    assert (receipt["accepted_source_count"], receipt["foreign_source_count"]) == (1, 1)


@pytest.mark.parametrize("stage", ["raw", "projection"])
def test_fsync_failure_keeps_partial_raw_and_never_publishes(tmp_path, monkeypatch, stage):
    cap, _ = capture(tmp_path)
    original = v3.os.fsync
    fd = cap._streams[stage].fileno()

    def failed(current):
        if current == fd:
            raise OSError(f"{stage} fsync failed")
        return original(current)

    with monkeypatch.context() as change:
        change.setattr(v3.os, "fsync", failed)
        with pytest.raises(OSError, match="fsync failed"):
            cap.append_batch([(Message(), 10.0)])
    assert not cap._latest and not cap._records
    with pytest.raises(RuntimeError, match="poisoned"):
        cap.append_batch([(Message(), 10.0)])
    receipt = cap.abort("failed fsync")
    assert receipt["status"] == "aborted" and receipt["record_count"] == 0
    assert receipt["orphan_packet_count"] == 1
    assert receipt["journal_sources"][stage]["uncommitted_tail_bytes"] > 0
    assert Message().payload in Path(receipt["journal_sources"]["raw"]["path"]).read_bytes()
    assert receipt["commit_counts"][stage]["fsync_returned"] == 0


class ShortStream:
    def __init__(self, stream):
        self.stream = stream

    def write(self, raw):
        return self.stream.write(raw[:1])

    def __getattr__(self, name):
        return getattr(self.stream, name)


@pytest.mark.parametrize("stage", ["raw", "projection"])
def test_short_append_not_hidden_or_rolled_back(tmp_path, stage):
    cap, _ = capture(tmp_path)
    cap._streams[stage] = ShortStream(cap._streams[stage])
    with pytest.raises(OSError, match="Short"):
        cap.append_batch([(Message(), 10.0)])
    receipt = cap.abort("short append")
    assert receipt["journal_sources"][stage]["uncommitted_tail_bytes"] == 1
    assert receipt["record_count"] == 0 and receipt["orphan_packet_count"] == 1


@pytest.mark.parametrize(
    "accessor", ["get_type", "get_srcSystem", "get_srcComponent", "get_seq", "to_dict"]
)
def test_decoder_exception_after_raw_commit_retains_prior_batch_not_new_cache(tmp_path, accessor):
    cap, _ = capture(tmp_path)
    cap.append_batch([(Message(x=11.0), 10.0)])
    bad = Message()

    def failed():
        raise MemoryError("actual decoded access failed")

    setattr(bad, accessor, failed)
    with pytest.raises(MemoryError):
        cap.append_batch([(Message(x=22.0), 10.01), (bad, 10.02)])
    assert cap._latest["LOCAL_POSITION_NED"]["message"]["x"] == 11.0
    receipt = cap.abort("decoder failure")
    assert receipt["record_count"] == 1 and receipt["packet_attempt_count"] == 3
    assert receipt["orphan_packet_count"] == 2
    assert receipt["commit_counts"]["raw"]["verified"] == 2
    assert receipt["commit_counts"]["projection"]["verified"] == 1
    assert all(a["status"] == "raw-committed" for a in receipt["packet_attempts"][1:])


@pytest.mark.parametrize("call", [2, 3, 4])
def test_late_clock_failure_after_commit_refuses_admission(tmp_path, call):
    clock = Clock()

    def failing_clock():
        value = clock()
        return 1.0 if clock.calls == call else value

    cap, _ = capture(tmp_path, clock=failing_clock)
    with pytest.raises(ValueError, match="regressed"):
        cap.append_batch([(Message(), 10.0)])
    assert cap._latest == {} and cap._records == []
    receipt = cap.abort("late clock")
    assert receipt["commit_counts"]["raw"]["verified"] == 1
    assert receipt["commit_counts"]["projection"]["verified"] == (0 if call == 2 else 1)
    assert receipt["orphan_packet_count"] == 1


def test_detachment_failure_after_dualcommit_never_returns_or_updates_cache(tmp_path, monkeypatch):
    cap, _ = capture(tmp_path)
    with monkeypatch.context() as change:
        change.setattr(v3.copy, "deepcopy", lambda _: (_ for _ in ()).throw(MemoryError("detach")))
        with pytest.raises(MemoryError, match="detach"):
            cap.append_batch([(Message(), 10.0)])
    assert not cap._records and not cap._latest
    assert cap.abort("detach failed")["commit_counts"]["projection"]["verified"] == 1


def test_packet_mutation_after_raw_fsync_refused_without_projection(tmp_path, monkeypatch):
    cap, _ = capture(tmp_path)
    message = Message()
    original = cap._append

    def mutate(stage, raw, batch):
        result = original(stage, raw, batch)
        if stage == "raw":
            message.payload = b"different bytes"
        return result

    monkeypatch.setattr(cap, "_append", mutate)
    with pytest.raises(ValueError, match="changed after raw commit"):
        cap.append_batch([(message, 10.0)])
    receipt = cap.abort("mutated")
    assert receipt["commit_counts"]["projection"]["attempted"] == 0
    assert (
        b"original-fixture-packet" in Path(receipt["journal_sources"]["raw"]["path"]).read_bytes()
    )


@pytest.mark.parametrize(
    "samples",
    [
        [],
        [None],
        [(Message(), True)],
        [(Message(), float("nan"))],
        [(Message(), 11.0)],
        [(Message(), 10.0)] * 17,
        [(Message(), 10.01), (Message(), 10.0)],
    ],
)
def test_invalid_batch_and_receive_times_fail_closed(tmp_path, samples):
    cap, _ = capture(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        cap.append_batch(samples)
    receipt = cap.abort("invalid batch")
    assert receipt["record_count"] == 0 and receipt["commit_counts"]["raw"]["attempted"] == 0


@pytest.mark.parametrize("payload", [b"", b"x" * 4097, memoryview(b"x"), None])
def test_packet_cap_and_type_rejected_before_write(tmp_path, payload):
    cap, _ = capture(tmp_path)
    message = Message()
    message.payload = payload
    with pytest.raises(ValueError, match="Bounded original"):
        cap.append_batch([(message, 10.0)])
    assert cap.abort("bad raw")["record_count"] == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"extra": memoryview(b"x")},
        {"extra": "x" * 20000},
        {"source_system_id": 1},
        {"mavpackettype": "OTHER"},
    ],
)
def test_unknown_or_colliding_projection_raw_is_durable(tmp_path, fields):
    cap, _ = capture(tmp_path)
    message = Message(**fields)
    with pytest.raises((ValueError, TypeError)):
        cap.append_batch([(message, 10.0)])
    receipt = cap.abort("bad projection")
    assert receipt["commit_counts"]["raw"]["verified"] == 1
    assert receipt["commit_counts"]["projection"]["attempted"] == 0
    assert receipt["record_count"] == 0


def test_record_limit_and_attempts_are_not_refunded(tmp_path):
    cap, _ = capture(tmp_path, max_records=1)
    cap.append_batch([(Message(), 10.0)])
    with pytest.raises(ValueError, match="record budget"):
        cap.append_batch([(Message(), 10.0)])
    receipt = cap.abort("capacity")
    assert receipt["record_count"] == receipt["packet_attempt_count"] == 1
    assert receipt["status"] == "aborted"


@pytest.mark.parametrize("stage", ["raw", "projection"])
def test_total_budget_checked_before_stage_no_truncation(tmp_path, monkeypatch, stage):
    cap, _ = capture(tmp_path)
    if stage == "raw":
        monkeypatch.setattr(v3, "MAX_TOTAL_BYTES", cap._charged_bytes + 1)
    else:
        original = cap._append

        def limit_after_raw(current, raw, batch):
            result = original(current, raw, batch)
            if current == "raw":
                monkeypatch.setattr(v3, "MAX_TOTAL_BYTES", cap._charged_bytes)
            return result

        monkeypatch.setattr(cap, "_append", limit_after_raw)
    with pytest.raises(ValueError, match="byte budget"):
        cap.append_batch([(Message(), 10.0)])
    receipt = cap.abort("capacity")
    assert receipt["record_count"] == 0
    assert receipt["commit_counts"][stage]["attempted"] == 0


def test_materialized_old_rows_have_original_receive_and_exact_source_hashes(tmp_path):
    cap, clock = capture(tmp_path)
    journal_row = cap.append_batch([(Message(time_boot_ms=987), 10.0)])[0]
    result = cap.materialize_snapshot(
        ["LOCAL_POSITION_NED"], tmp_path / "snapshot", now_s=clock.value
    )
    row = result["LOCAL_POSITION_NED"]
    assert row["received_at_s"] == journal_row["received_at_s"] == 10.0
    assert row["message"]["time_boot_ms"] == 987
    assert row["dispatch_ready_at_s"] > journal_row["projection_committed_at_s"]
    assert set(row) == {
        "record_index",
        "received_at_s",
        "clock_id",
        "transaction_id",
        "local_origin_id",
        "system_id",
        "component_id",
        "packet_sequence",
        "packet_sha256",
        "source_accepted",
        "message",
        "claims",
        "dispatch_ready_at_s",
        "envelope",
        "packet",
    }
    envelope = json.loads(Path(row["envelope"]["path"]).read_bytes())
    assert envelope == {
        k: v for k, v in row.items() if k not in {"packet", "envelope", "dispatch_ready_at_s"}
    }
    manifest = json.loads((tmp_path / "snapshot/snapshot-manifest.json").read_bytes())
    assert manifest["received_times_renewed"] is False
    assert manifest["selected"]["LOCAL_POSITION_NED"]["raw_record"] == journal_row["raw_record"]
    result["LOCAL_POSITION_NED"]["message"]["x"] = 333
    assert cap._latest["LOCAL_POSITION_NED"]["message"]["x"] == 0
    receipt = cap.seal()
    assert receipt["snapshots"][0]["status"] == "materialized"
    data_bytes = sum(
        p.stat().st_size
        for p in [*(tmp_path / "journals").iterdir(), *(tmp_path / "snapshot").iterdir()]
        if p.name != "capture-receipt.json"
    )
    assert (
        receipt["data_budget_charged_bytes"] == receipt["verified_durable_data_bytes"] == data_bytes
    )


@pytest.mark.parametrize("failure", ["missing", "stale", "future", "output_exists", "duplicates"])
def test_materialization_refuses_missing_stale_future_and_overwrite(tmp_path, failure):
    cap, clock = capture(tmp_path)
    cap.append_batch([(Message(), 10.0)])
    kinds, now, out = ["LOCAL_POSITION_NED"], clock.value, tmp_path / "snapshot"
    if failure == "missing":
        kinds = ["HEARTBEAT"]
    elif failure == "stale":
        clock.value = now = 15.0
    elif failure == "future":
        now = 999.0
    elif failure == "output_exists":
        out.mkdir()
        (out / "user.bin").write_bytes(b"untouched")
    else:
        kinds *= 2
    with pytest.raises((ValueError, FileExistsError)):
        cap.materialize_snapshot(kinds, out, now_s=now)
    receipt = cap.abort("snapshot failure")
    assert receipt["record_count"] == 1 and receipt["snapshots"][0]["status"] == "refused"
    if failure == "output_exists":
        assert (out / "user.bin").read_bytes() == b"untouched"


@pytest.mark.parametrize("stage", ["raw", "projection"])
def test_journal_source_drift_before_snapshot_refused(tmp_path, stage):
    cap, clock = capture(tmp_path)
    cap.append_batch([(Message(), 10.0)])
    path = Path(cap._streams[stage].name)
    raw = path.read_bytes()
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    with pytest.raises(ValueError, match="Journals differ"):
        cap.materialize_snapshot(["LOCAL_POSITION_NED"], tmp_path / "out", now_s=clock.value)
    receipt = cap.abort("drift")
    assert receipt["journal_sources"][stage]["verified_prefix_matches"] is False
    assert not (tmp_path / "out").exists()


def test_materialization_late_clock_preserves_partial_files_without_return(tmp_path, monkeypatch):
    cap, clock = capture(tmp_path)
    cap.append_batch([(Message(), 10.0)])
    original = cap._save_new

    def delayed(path, raw):
        result = original(path, raw)
        if path.suffix == ".json":
            clock.value = 20.0
        return result

    monkeypatch.setattr(cap, "_save_new", delayed)
    with pytest.raises(ValueError, match="stale during materialization"):
        cap.materialize_snapshot(["LOCAL_POSITION_NED"], tmp_path / "out", now_s=clock.value)
    receipt = cap.abort("late export")
    assert len(receipt["snapshots"][0]["files"]) == 2
    assert receipt["snapshots"][0]["status"] == "refused"
    assert not (tmp_path / "out/snapshot-manifest.json").exists()


def test_final_close_rechecks_exported_bytes_and_preserves_failure_receipt(tmp_path):
    cap, clock = capture(tmp_path)
    cap.append_batch([(Message(), 10.0)])
    rows = cap.materialize_snapshot(["LOCAL_POSITION_NED"], tmp_path / "out", now_s=clock.value)
    Path(rows["LOCAL_POSITION_NED"]["packet"]["path"]).write_bytes(b"wrong")
    with pytest.raises(RuntimeError, match="Materialized evidence changed"):
        cap.seal()
    receipt = json.loads((cap.directory / "capture-receipt.json").read_bytes())
    assert receipt["status"] == "aborted" and receipt["evidence_error"]
    assert all(stream.closed for stream in cap._streams.values())


def test_snapshot_detachment_delay_still_uses_original_receive_age(tmp_path, monkeypatch):
    cap, clock = capture(tmp_path)
    cap.append_batch([(Message(), 10.0)])
    original = v3.copy.deepcopy

    def delayed(value):
        result = original(value)
        if type(value) is dict and set(value) == {"LOCAL_POSITION_NED"}:
            clock.value = 20.0
        return result

    with monkeypatch.context() as change:
        change.setattr(v3.copy, "deepcopy", delayed)
        with pytest.raises(ValueError, match="stale during final source checks"):
            cap.materialize_snapshot(["LOCAL_POSITION_NED"], tmp_path / "out", now_s=clock.value)
    receipt = cap.abort("detachment became stale")
    assert receipt["snapshots"][0]["status"] == "refused"
    assert receipt["snapshots"][0]["manifest"]["bytes"] > 0
    assert cap._records[0]["received_at_s"] == 10.0


def test_codec_source_and_post_commit_drift_rejected_before_cache(tmp_path, monkeypatch):
    cap, _ = capture(tmp_path)
    original = v3._verify_codec
    calls = []

    def drift():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("Frozen V2 source differs")
        original()

    with monkeypatch.context() as change:
        change.setattr(v3, "_verify_codec", drift)
        with pytest.raises(RuntimeError, match="source differs"):
            cap.append_batch([(Message(), 10.0)])
    assert not cap._latest and not cap._records
    assert cap.abort("source drift")["commit_counts"]["projection"]["verified"] == 1
    bad = tmp_path / "wrong-codec.py"
    bad.write_bytes(b"bad source")
    monkeypatch.setattr(v3, "_CODEC_PATH", bad)
    with pytest.raises(RuntimeError, match="V2 codec source differs"):
        v3._verify_codec()


def test_owner_reentrancy_and_once_only_close(tmp_path):
    cap, _ = capture(tmp_path)
    errors = []

    def foreign():
        try:
            cap.append_batch([(Message(), 10.0)])
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join()
    assert errors and not cap._records
    cap.abort("wrong writer")
    with pytest.raises(RuntimeError, match="only once"):
        cap.abort("second")
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    recursive, _ = capture(second_dir)
    msg = Message()
    msg.to_dict = lambda: recursive.append_batch([(Message(), 10.0)])
    with pytest.raises(RuntimeError, match="Reentrant"):
        recursive.append_batch([(msg, 10.0)])
    assert recursive.abort("reentrant")["record_count"] == 0


def test_all_nine_materialized_rows_pass_unmodified_frozen_context_bridge(tmp_path):
    from test_onboard_teacher_observed_context import fixture as native_fixture

    fixture = native_fixture.__wrapped__(tmp_path)
    clock = Clock(10.1)
    cap = v3.TeacherTelemetryCaptureV3(
        tmp_path / "v3",
        clock=clock,
        clock_id=fixture["clock_id"],
        transaction_id=fixture["transaction_id"],
        local_origin_id=fixture["local_origin_id"],
    )
    samples = []
    for kind, row in fixture["rows"].items():
        message = Message(kind=kind)
        message.fields = {
            k: copy.deepcopy(v)
            for k, v in row["message"].items()
            if k not in {"message_type", "source_system_id", "source_component_id"}
        }
        samples.append((message, 10.0))
    cap.append_batch(samples)
    fixture["rows"] = cap.materialize_snapshot(
        list(observed.REQUIRED_MESSAGES), tmp_path / "snapshot", now_s=clock.value
    )
    result = observed.build_observed_teacher_context(**fixture)
    assert result.context.aircraft_position.latitude_deg == pytest.approx(1.3)
    assert not result.to_mapping()["claims"]["hardware_control_enabled"]
    assert len(cap.seal()["snapshots"][0]["files"]) == 18


def test_actual_bad_data_primary_bytes_held_but_foreign(tmp_path):
    from test_onboard_teacher_telemetry_capture_v2 import installed_dialect

    primary = installed_dialect.__wrapped__()
    message = primary.MAVLink_bad_data(bytearray(b"\x00\xffbad"), "CPU fixture")
    cap, _ = capture(tmp_path)
    row = cap.append_batch([(message, 10.0)])[0]
    assert row["system_id"] == row["component_id"] == 0
    assert row["source_accepted"] is False and not cap._latest
    document = projection_document(cap, row)
    assert_bits(message.to_dict(), v3._codec.decode_typed_value(document["typed_value"]))
    assert cap.seal()["foreign_source_count"] == 1


def test_fresh_stdlib_import_does_not_load_optional_runtime():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys;sys.path[:0]=" + repr([str(root), str(root / "src")]) + ";"
        "from scripts import onboard_teacher_telemetry_capture_v3 as v;"
        "assert not any(n.split('.')[0] in "
        "{'torch','PIL','numpy','pymavlink','transformers'} for n in sys.modules);"
        "assert v.MAX_BATCH==16 and v.CODEC_SHA256=='" + v3.CODEC_SHA256 + "'"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, timeout=15
    )
    assert completed.returncode == 0, completed.stderr

"""CPU receive/persistence fixtures, not actual MAVLink/Gazebo or expert evidence."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from scripts.onboard_teacher_telemetry_capture import (
    FALSE_CLAIMS,
    TeacherTelemetryCapture,
)


class Message:
    def __init__(self, kind="LOCAL_POSITION_NED", system=1, component=1, **fields):
        self.kind, self.system, self.component = kind, system, component
        self.fields = {
            "mavpackettype": kind,
            "time_boot_ms": 123,
            "x": 0.0,
            "y": 1.0,
            "z": -12.0,
            "vx": 0.0,
            "vy": 0.0,
            "vz": 0.0,
            **fields,
        }
        self.payload = b"original-fixture-packet"

    def get_srcSystem(self):
        return self.system

    def get_srcComponent(self):
        return self.component

    def get_seq(self):
        return 255

    def get_type(self):
        return self.kind

    def get_msgbuf(self):
        return self.payload

    def to_dict(self):
        return self.fields


def make_capture(tmp_path, messages=(), **options):
    queue, calls = list(messages), []
    times = [10.0]

    def clock():
        times[0] += 0.001
        return times[0]

    def reader(timeout):
        calls.append(timeout)
        return queue.pop(0) if queue else None

    capture = TeacherTelemetryCapture(
        tmp_path / "raw",
        reader=options.pop("reader", reader),
        clock=options.pop("clock", clock),
        clock_id="same-host-qpc",
        transaction_id="owned-sim-tx",
        local_origin_id="declared-ekf-origin",
        **options,
    )
    return capture, calls


def test_foreign_packet_persisted_but_not_returned_or_cached(tmp_path):
    foreign, expected = Message(system=2, x=999.0), Message()
    cap, calls = make_capture(tmp_path, [foreign, expected])
    message, row = cap.receive(1)
    assert message is expected and len(calls) == 2
    assert row["record_index"] == 1 and row["message"]["x"] == 0
    latest = cap.latest_message("LOCAL_POSITION_NED", now_s=10.02, maximum_age_s=1)
    assert latest["message"]["x"] == 0
    receipt = cap.seal()
    assert receipt["record_count"] == 2
    assert receipt["foreign_source_count"] == receipt["accepted_source_count"] == 1
    assert not any(receipt["claims"].values())
    original = json.loads(Path(receipt["records"][0]["envelope"]["path"]).read_text())
    assert original["message"]["x"] == 999 and original["source_accepted"] is False
    assert original["message"]["time_boot_ms"] == 123


def test_raw_packet_fields_and_copy_detachment(tmp_path):
    source = Message(kind="ATTITUDE_QUATERNION", q1=1.0, q2=0.2, q3=0.3, q4=0.4)
    cap, _ = make_capture(tmp_path, [source])
    _, row = cap.receive(1)
    assert Path(row["packet"]["path"]).read_bytes() == source.payload
    assert row["packet_sha256"] == hashlib.sha256(source.payload).hexdigest()
    assert row["dispatch_ready_at_s"] >= row["received_at_s"]
    source.fields["q2"] = row["message"]["q3"] = 99
    latest = cap.latest_message("ATTITUDE_QUATERNION", now_s=10.02, maximum_age_s=1)
    assert latest["message"]["q2"] == 0.2 and latest["message"]["q3"] == 0.3
    latest["message"]["q4"] = 99
    assert (
        cap.latest_message("ATTITUDE_QUATERNION", now_s=10.02, maximum_age_s=1)["message"]["q4"]
        == 0.4
    )
    cap.seal()


def test_no_reordering_ahead_reached_or_boot_clock_projection(tmp_path):
    messages = [
        Message("MISSION_ITEM_REACHED", seq=2),
        Message("MISSION_CURRENT", seq=1),
        Message(time_boot_ms=122),
    ]
    cap, _ = make_capture(tmp_path, messages)
    seen = [cap.receive(1)[1]["message"] for _ in messages]
    assert [row["message_type"] for row in seen] == [row.kind for row in messages]
    assert seen[0]["seq"] == 2 and seen[1]["seq"] == 1 and seen[2]["time_boot_ms"] == 122
    assert cap.seal()["status"] == "recorded"  # No interval or teacher completion asserted.


def test_existing_interval_consumer_uses_same_persisted_source_projection(tmp_path):
    from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
    from scripts.onboard_teacher_mission_intervals import (
        MissionIntervalRecorder,
        MissionParentSubmission,
    )

    cap, _ = make_capture(
        tmp_path, [Message("MISSION_CURRENT", seq=1), Message("MISSION_ITEM_REACHED", seq=1)]
    )
    parent = MissionParentSubmission(
        "fixture-parent",
        cap.transaction_id,
        "fixture-vehicle",
        "a" * 64,
        "b" * 64,
        5,
        9.0,
        cap.clock_id,
        1,
        1,
        cap.local_origin_id,
        EvidenceBinding("fixture/submission.json", 10, "c" * 64, "fixture-submit", ""),
    )
    recorder = MissionIntervalRecorder(parent, (1,))
    for _ in range(2):
        _, row = cap.receive(1)
        saved = json.loads(Path(row["envelope"]["path"]).read_text())
        assert saved["message"] == row["message"]
        source = EvidenceBinding(
            "raw/" + Path(row["envelope"]["path"]).name,
            row["envelope"]["bytes"],
            row["envelope"]["sha256"],
            "fixture-one-receiver",
            "",
        )
        assert recorder.record_feedback(
            row["message"],
            received_at_s=row["received_at_s"],
            clock_id=row["clock_id"],
            transaction_id=row["transaction_id"],
            source=source,
            packet_sha256=row["packet_sha256"],
        )
    result = recorder.close()
    assert result["valid"] and result["intervals"][0]["status"] == "incomplete"
    assert not any(result["claims"].values())
    cap.seal()


@pytest.mark.parametrize("now,age", [(9, 1), (12, 1), (11, 0)])
def test_latest_future_or_stale_is_absent(tmp_path, now, age):
    cap, _ = make_capture(tmp_path, [Message()])
    cap.receive(1)
    assert cap.latest_message("LOCAL_POSITION_NED", now_s=now, maximum_age_s=age) is None
    cap.seal()


def test_none_and_foreign_timeout_never_invent_state(tmp_path):
    cap, calls = make_capture(tmp_path, [Message(component=2)])
    assert cap.receive(0) is None
    assert len(calls) == 1
    assert cap.latest_message("LOCAL_POSITION_NED", now_s=10.02, maximum_age_s=1) is None
    assert cap.receive(1) is None
    assert cap.seal()["record_count"] == 1


@pytest.mark.parametrize(
    "variant",
    [
        "nan",
        "bad_fields",
        "empty",
        "oversize",
        "type",
        "bool_source",
        "metadata_collision",
        "unsupported",
    ],
)
def test_invalid_packet_poison_no_cache_or_success_receipt(tmp_path, variant):
    message = Message()
    if variant == "nan":
        message.fields["x"] = float("nan")
    elif variant == "bad_fields":
        message.fields["mavpackettype"] = "HEARTBEAT"
    elif variant == "empty":
        message.payload = b""
    elif variant == "oversize":
        message.payload = b"x" * 4097
    elif variant == "type":
        message.kind = "lowercase"
    elif variant == "bool_source":
        message.system = True
    elif variant == "metadata_collision":
        message.fields["system_id"] = 7
    else:
        message.fields["opaque"] = object()
    cap, _ = make_capture(tmp_path, [message])
    with pytest.raises((ValueError, TypeError)):
        cap.receive(1)
    with pytest.raises(RuntimeError):
        cap.latest_message("LOCAL_POSITION_NED", now_s=10.01, maximum_age_s=1)
    with pytest.raises(RuntimeError):
        cap.seal()
    assert cap.abort("invalid input")["status"] == "aborted"


def test_record_capacity_exhaustion_keeps_original_not_silently_truncated(tmp_path):
    cap, _ = make_capture(tmp_path, [Message(), Message()], max_records=1)
    cap.receive(1)
    with pytest.raises(ValueError, match="capacity"):
        cap.receive(1)
    receipt = cap.abort("full")
    assert receipt["record_count"] == 1 and receipt["status"] == "aborted"
    assert "capacity" in receipt["error"]


def test_monotonic_observer_failure(tmp_path):
    times = iter([10, 10, 9])
    cap, _ = make_capture(tmp_path, [Message()], clock=lambda: next(times))
    with pytest.raises(ValueError, match="regressed"):
        cap.receive(1)
    assert cap.abort("clock")["record_count"] == 0


def test_failure_to_publish_poison_and_preserve_existing_file(tmp_path):
    cap, _ = make_capture(tmp_path, [Message()])
    target = cap.directory / "receive-00000.bin"
    target.write_bytes(b"existing-user-fixture")
    with pytest.raises(FileExistsError):
        cap.receive(1)
    assert target.read_bytes() == b"existing-user-fixture"
    assert cap.abort("write failure")["record_count"] == 0


def test_existing_directory_and_double_close_rejected(tmp_path):
    cap, _ = make_capture(tmp_path)
    with pytest.raises(FileExistsError):
        make_capture(tmp_path)
    cap.seal()
    with pytest.raises(RuntimeError):
        cap.abort("closed")
    with pytest.raises(RuntimeError):
        cap.receive(1)


def test_wrong_thread_and_reentrant_reader_poison(tmp_path):
    cap, _ = make_capture(tmp_path)
    errors = []

    def wrong_thread():
        try:
            cap.receive(1)
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=wrong_thread)
    thread.start()
    thread.join()
    assert len(errors) == 1 and cap.abort("wrong owner")["status"] == "aborted"
    recursive = None

    def reader(_timeout):
        return recursive.receive(0)

    (tmp_path / "other").mkdir()
    recursive, _ = make_capture(tmp_path / "other", reader=reader)
    with pytest.raises(RuntimeError, match="Reentrant"):
        recursive.receive(1)
    assert recursive.abort("reentry")["status"] == "aborted"


def test_no_optional_ml_import_or_flight_facility():
    import ast

    source = Path(__file__).parents[1] / "scripts/onboard_teacher_telemetry_capture.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports |= {
        item.name for node in ast.walk(tree) if isinstance(node, ast.Import) for item in node.names
    }
    assert imports == {
        "__future__",
        "copy",
        "hashlib",
        "os",
        "re",
        "threading",
        "pathlib",
        "multidetect.autonomy.serialization",
    }
    assert not any(FALSE_CLAIMS.values())

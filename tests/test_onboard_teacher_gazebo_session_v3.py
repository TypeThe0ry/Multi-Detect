"""Injected CPU owner/cleanup tests; no Docker, socket, camera or model run."""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.clock import HighResolutionSystemClock
from scripts import onboard_teacher_gazebo_session_v3 as v3

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "_frozen_teacher_session_test_helpers",
    ROOT / "tests/test_onboard_teacher_gazebo_session.py",
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return helpers.docker_template.__wrapped__(tmp_path_factory)


@pytest.fixture
def plan(tmp_path, template):
    return helpers.plan.__wrapped__(tmp_path, template)


def session(plan):
    # Only the injected camera-worker seam is exercised; __enter__ is NOT called.
    result = v3.OwnedTeacherGazeboSessionV3(
        plan,
        execute=True,
        clock=HighResolutionSystemClock(),
        camera_factory=lambda *_: SimpleNamespace(close=lambda: None),
    )
    result._owner = threading.get_ident()
    result.deadline_s = result.clock.monotonic() + 5
    result.container_id = helpers.IDENTITY  # stand-in argument, not a real owned container
    return result


class ThreadStandin:
    def __init__(self, *, survives=False):
        self.alive = True
        self.survives = survives
        self.joined = []

    def is_alive(self):
        return self.alive

    def join(self, timeout):
        self.joined.append(timeout)
        if not self.survives:
            self.alive = False


def test_stop_camera_close_exception_still_joins_owned_thread(plan):
    current = session(plan)
    thread = current._camera_thread = ThreadStandin()
    calls = []

    def close(*, require_complete):
        calls.append(require_complete)
        raise OSError("camera-close-primary")

    current._active_camera = SimpleNamespace(close=close)
    with pytest.raises((OSError, v3.TeacherSessionError)) as captured:
        current.stop_camera()
    assert "camera-close-primary" in str(captured.value)
    assert calls == [False] and thread.joined == [4]
    assert current._camera_stop.is_set() and not thread.is_alive()


def test_stop_camera_alive_after_join_cannot_succeed(plan):
    current = session(plan)
    current._camera_thread = ThreadStandin(survives=True)
    closed = []
    current._active_camera = SimpleNamespace(close=lambda **kw: closed.append(kw))
    with pytest.raises(v3.TeacherSessionError, match="did not exit"):
        current.stop_camera()
    assert closed == [{"require_complete": False}]
    assert current._camera_thread.joined == [4]
    assert current.to_mapping()["camera_thread_alive"] is True


def test_stop_camera_no_active_camera_still_joins(plan):
    current = session(plan)
    thread = current._camera_thread = ThreadStandin()
    current.stop_camera()
    assert thread.joined == [4] and not thread.is_alive()


def test_stop_camera_absent_or_already_exited_does_not_create_work(plan):
    current = session(plan)
    current.stop_camera()
    assert current._camera_thread is None
    current._camera_thread = ThreadStandin()
    current._camera_thread.alive = False
    current.stop_camera()
    assert current._camera_thread.joined == []


@pytest.mark.parametrize("stage", ["read", "persist"])
@pytest.mark.parametrize("secondary", ["close", "stats"])
def test_window_primary_error_not_replaced_by_cleanup_secondary(
    tmp_path, plan, monkeypatch, stage, secondary
):
    current = session(plan)
    primary = MemoryError(f"{stage}-original-primary")
    calls = []

    class Stats:
        def to_mapping(self):
            calls.append("stats")
            if secondary == "stats":
                raise OSError("stats-secondary")
            return {"helper_exit_code": 0, "complete": True}

    class Camera:
        def __init__(self, *_):
            self.source = SimpleNamespace(stats=Stats())

        def read(self):
            calls.append("read")
            if stage == "read":
                raise primary
            return SimpleNamespace(
                host_return_stamp=SimpleNamespace(value_ns=10000000000),
                provenance=SimpleNamespace(sensor_stamp_ns=123),
            )

        def close(self, *, require_complete):
            calls.append(("close", require_complete))
            if secondary == "close":
                raise OSError("close-secondary")

    def persist(*_):
        calls.append("persist")
        raise primary

    monkeypatch.setattr(v3, "BoundedTeacherCamera", Camera)
    current.start_camera_windows(tmp_path, persist, windows=1, interval_s=0)
    current._camera_thread.join(2)
    assert not current._camera_thread.is_alive()
    assert f"MemoryError: {stage}-original-primary" in current._window_report["failure"]
    assert current._window_report["complete"] is False
    assert current._window_report["windows"][0]["complete"] is False
    assert current._active_camera is None
    assert ("close", False) in calls
    assert any(f"{secondary}-secondary" in note for note in primary.__notes__)
    with pytest.raises(v3.TeacherSessionError, match="original-primary"):
        current.camera_finished()
    current.stop_camera()


def test_successful_window_is_complete_only_after_close_and_native_stats(
    tmp_path, plan, monkeypatch
):
    current = session(plan)
    calls = []

    class Camera:
        def __init__(self, *_):
            self.source = SimpleNamespace(
                stats=SimpleNamespace(to_mapping=lambda: {"helper_exit_code": 0, "complete": True})
            )

        def read(self):
            calls.append("read")
            return SimpleNamespace(
                host_return_stamp=SimpleNamespace(value_ns=10000000000 + len(calls)),
                provenance=SimpleNamespace(sensor_stamp_ns=123 + len(calls)),
            )

        def close(self, *, require_complete):
            calls.append(("close", require_complete))

    monkeypatch.setattr(v3, "BoundedTeacherCamera", Camera)
    current.start_camera_windows(
        tmp_path, lambda target, _: {"fixture_path": str(target)}, windows=1, interval_s=0
    )
    current._camera_thread.join(2)
    assert not current._camera_thread.is_alive() and current.camera_finished()
    updates = current.camera_updates()
    assert len(updates) == 2 and [r["frame_index"] for r in updates] == [0, 1]
    assert all(r["source_sensor_stamp_ns"] > 0 for r in updates)
    assert calls.count(("close", True)) >= 1
    assert current._window_report["windows"][0]["native_stats"]["helper_exit_code"] == 0
    assert current.to_mapping()["flight_authority"] is False
    assert current.camera_updates() == []


class RawMessage:
    def __init__(self):
        self.packet = bytearray(b"original-wire")
        self.access = []

    def get_msgbuf(self):
        self.access.append(("raw", threading.get_ident()))
        return self.packet

    def get_type(self):
        self.access.append(("decoded", threading.get_ident()))
        return "HEARTBEAT"

    def to_dict(self):
        self.access.append(("projection", threading.get_ident()))
        return {"mavpackettype": "HEARTBEAT"}


def test_frozen_received_message_clones_only_raw_and_defers_decoded_access():
    original = RawMessage()
    frozen = v3.FrozenReceivedMessage(original)
    assert [kind for kind, _ in original.access] == ["raw"]
    assert frozen.get_msgbuf() == b"original-wire"
    assert [kind for kind, _ in original.access] == ["raw"]
    assert frozen.get_type() == "HEARTBEAT"
    assert frozen.to_dict() == {"mavpackettype": "HEARTBEAT"}
    assert [kind for kind, _ in original.access] == ["raw", "raw", "decoded", "raw", "projection"]


def test_frozen_received_message_rejects_mutated_decoder_without_getter():
    original = RawMessage()
    frozen = v3.FrozenReceivedMessage(original)
    original.packet[0] ^= 1
    assert frozen.get_msgbuf() == b"original-wire"
    with pytest.raises(RuntimeError, match="packet changed"):
        frozen.get_type()
    assert not any(kind == "decoded" for kind, _ in original.access)
    with pytest.raises(AttributeError):
        frozen.send_command()


@pytest.mark.parametrize("raw", [b"", b"x" * 4097])
def test_frozen_received_message_raw_budget_rejected(raw):
    original = RawMessage()
    original.packet = raw
    with pytest.raises(ValueError, match="Bounded original"):
        v3.FrozenReceivedMessage(original)


def test_requests_and_raw_receive_share_one_thread_without_decoded_RX_access():
    main = threading.get_ident()
    original = RawMessage()
    reads, sends = [], []
    stop_wait = threading.Event()
    pending = [original]

    def reader(_):
        reads.append(threading.get_ident())
        if pending:
            return pending.pop()
        stop_wait.wait(0.005)
        return None

    def sender(names):
        sends.append((threading.get_ident(), names))
        return [{"name": name, "acknowledged": False} for name in names]

    receiver = v3.ConcurrentTelemetryReceiver(
        reader, lambda: 10.0, request_sender=sender, queue_capacity=4
    ).start()
    try:
        batch = receiver.read_batch(0.2)
        assert len(batch) == 1 and batch[0][1] == 10.0
        assert [kind for kind, _ in original.access] == ["raw"]
        rows = receiver.request_streams(["HEARTBEAT", "GPS_RAW_INT"])
        assert [r["name"] for r in rows] == ["HEARTBEAT", "GPS_RAW_INT"]
        assert len({*reads, *(owner for owner, _ in sends)}) == 1
        assert reads[0] != main and sends[0][0] == reads[0]
        assert [owner for kind, owner in original.access if kind == "raw"] == [reads[0]]
        assert batch[0][0].get_type() == "HEARTBEAT"
        assert original.access[-1] == ("decoded", main)
        with pytest.raises(ValueError, match="allowlisted"):
            receiver.request_streams(["ARM"])
        assert len(sends) == 1
    finally:
        stop_wait.set()
        receiver.stop()
    report = receiver.to_mapping()
    assert report["thread_exited"] and not report["thread_alive"]
    assert report["request_API_calls"] == 2 and not report["flight_authority"]


def test_failed_sender_keeps_attempt_uncertainty_and_stops_owner():
    wait = threading.Event()
    calls = []

    def sender(names):
        calls.append(names)
        raise OSError("send returned no delivery result")

    receiver = v3.ConcurrentTelemetryReceiver(
        lambda _: (wait.wait(0.005), None)[1], lambda: 10.0, request_sender=sender
    ).start()
    try:
        with pytest.raises(OSError, match="no delivery result"):
            receiver.request_streams(["HEARTBEAT"])
        with pytest.raises(RuntimeError, match="no delivery result"):
            receiver.raise_if_failed()
    finally:
        wait.set()
        receiver.stop()
    report = receiver.to_mapping()
    assert calls == [("HEARTBEAT",)]
    assert report["request_API_calls"] == 0
    assert report["request_partial_API_calls_unknown"] is True
    assert report["thread_exited"] and not report["thread_alive"]

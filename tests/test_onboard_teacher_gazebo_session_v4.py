"""Small CPU stand-ins: no Docker, sockets, actual PX4, camera or models."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.clock import HighResolutionSystemClock
from scripts import onboard_teacher_gazebo_session_v4 as v4

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "_v4_frozen_session_test_helpers", ROOT / "tests/test_onboard_teacher_gazebo_session.py"
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return helpers.docker_template.__wrapped__(tmp_path_factory)


@pytest.fixture
def plan(tmp_path, template):
    return helpers.plan.__wrapped__(tmp_path, template)


class FakeMav:
    def __init__(self):
        self.calls = []
        self.on_call = None

    def _send(self, method, args):
        self.calls.append((method, args, threading.get_ident()))
        if self.on_call is not None:
            self.on_call(method, args)

    def param_request_read_send(self, *args):
        self._send("param_request_read_send", args)

    def command_long_send(self, *args):
        self._send("command_long_send", args)


class RawMessage:
    def __init__(self, packet=b"standin-original-packet"):
        self.packet, self.decoded_threads = packet, []

    def get_msgbuf(self):
        return self.packet

    def get_type(self):
        self.decoded_threads.append(threading.get_ident())
        return "PARAM_VALUE"

    def get_srcSystem(self):
        return 1

    def get_srcComponent(self):
        return 1

    def get_seq(self):
        return 4

    def to_dict(self):
        self.decoded_threads.append(threading.get_ident())
        return {"mavpackettype": "PARAM_VALUE", "param_id": "BAT1_CAPACITY"}


class Connection:
    def __init__(self):
        self.mav, self.inbox = FakeMav(), queue.Queue()
        self.read_threads, self.closed = [], False

    def recv_match(self, *, blocking, timeout):
        assert blocking is True and timeout > 0
        self.read_threads.append(threading.get_ident())
        try:
            return self.inbox.get(timeout=min(timeout, 0.01))
        except queue.Empty:
            return None

    def close(self):
        self.closed = True


def sender(connection=None, clock=time.monotonic, remaining=lambda: 5):
    connection = connection or Connection()
    return v4.ReadOnlyDiagnosticSender(connection, clock, "fixture-QPC", remaining)


def receiver(connection=None, *, capacity=256, send=None):
    connection = connection or Connection()
    given = send or sender(connection)
    return (
        v4.ConcurrentTelemetryReceiverV4(
            lambda timeout: connection.recv_match(blocking=True, timeout=timeout),
            time.monotonic,
            request_sender=given,
            queue_capacity=capacity,
        ),
        connection,
        given,
    )


def wait_until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "CPU fixture thread did not finish"
        time.sleep(0.001)


@pytest.mark.parametrize("name", v4.PARAMETER_ALLOWLIST)
def test_allowlisted_parameter_uses_exact_read_api_ascii_and_index(name):
    current = sender()
    rows = current((v4.DiagnosticRequest("parameter-read", name),))
    assert current.connection.mav.calls[0][:2] == (
        "param_request_read_send",
        (1, 1, name.encode("ascii"), -1),
    )
    assert rows[0]["API_returned"] is True
    assert rows[0]["param_id_ascii_hex"] == name.encode("ascii").hex()
    assert rows[0]["args"] == [1, 1, name, -1]
    assert rows[0]["parameter_write_performed"] is False
    assert rows[0]["response_observed_here"] is False
    assert rows[0]["flight_authority"] is False
    json.dumps(rows, allow_nan=False)


@pytest.mark.parametrize(
    "kind,name",
    [
        (True, None),
        (1, None),
        ("parameter-set", "BAT1_CAPACITY"),
        ("arm", None),
        ("mission-upload", None),
        ("parameter-read", None),
        ("parameter-read", b"BAT1_CAPACITY"),
        ("parameter-read", "UNLISTED"),
        ("stream-interval", "PARAM_VALUE"),
        ("autopilot-version", "AUTOPILOT_VERSION"),
    ],
)
def test_untyped_or_unallowlisted_requests_reject_before_send(kind, name):
    with pytest.raises((ValueError, TypeError)):
        v4.DiagnosticRequest(kind, name)


@pytest.mark.parametrize("values", [None, {}, (), [], [True], [{"kind": "parameter-read"}]])
def test_request_sequence_has_exact_types_and_nonempty_budget(values):
    current = sender()
    with pytest.raises((ValueError, TypeError)):
        current(values)
    assert current.to_mapping() == [] and current.connection.mav.calls == []


def test_mutated_typed_request_and_duplicate_batch_fail_before_send():
    current = sender()
    request = v4.DiagnosticRequest("parameter-read", "BAT1_CAPACITY")
    with pytest.raises(ValueError, match="Duplicate"):
        current([request, request])
    object.__setattr__(request, "name", "PARAM_SET")
    with pytest.raises(ValueError, match="allowlist"):
        current([request])
    assert current.to_mapping() == []


def test_three_branches_version_is_512_148_not_stream_511():
    current = sender()
    rows = current(
        [
            v4.DiagnosticRequest("autopilot-version"),
            v4.DiagnosticRequest("stream-interval", "BATTERY_STATUS"),
            v4.DiagnosticRequest("parameter-read", "BAT1_CAPACITY"),
        ]
    )
    assert current.connection.mav.calls[0][:2] == (
        "command_long_send",
        (1, 1, 512, 0, 148, 0, 0, 0, 0, 0, 0),
    )
    assert current.connection.mav.calls[1][:2] == (
        "command_long_send",
        (1, 1, 511, 0, 147, 250000, 0, 0, 0, 0, 0),
    )
    assert [r["message_type"] for r in rows] == [
        "AUTOPILOT_VERSION",
        "BATTERY_STATUS",
        "PARAM_VALUE",
    ]
    assert all(r["acknowledged"] is False for r in rows)
    assert all(r["API_returned_at_s"] >= r["API_called_at_s"] for r in rows)
    assert len({r["request_id"] for r in rows}) == 3


def test_attempt_is_in_memory_before_api_but_never_claimed_durable_or_delivered():
    current = sender()
    observed = []

    def check(*_):
        row = current.to_mapping()[0]
        assert row["API_attempted"] is True and row["API_returned"] is False
        assert row["attempt_evidence_saved_in_memory_before_API"] is True
        assert row["attempt_evidence_durable_before_API"] is False
        assert row["datagram_delivery_independently_verified"] is False
        observed.append(row)

    current.connection.mav.on_call = check
    rows = current([v4.DiagnosticRequest("autopilot-version")])
    assert len(observed) == 1 and rows[0]["API_returned"] is True
    rows[0]["args"].clear()
    assert current.to_mapping()[0]["args"]


def test_missing_api_retains_prepared_but_not_called_attempt():
    current = sender(SimpleNamespace(mav=SimpleNamespace()))
    with pytest.raises(AttributeError):
        current([v4.DiagnosticRequest("parameter-read", "SYS_AUTOSTART")])
    row = current.to_mapping()[0]
    assert row["API_attempted"] is False and row["API_returned"] is False
    assert row["error"].startswith("AttributeError:")


def test_partial_failure_retains_success_and_failed_attempt_not_unreached_request():
    current = sender()
    primary = OSError("second-exact-error")

    def fail_second(*_):
        if len(current.connection.mav.calls) == 2:
            raise primary

    current.connection.mav.on_call = fail_second
    with pytest.raises(OSError) as caught:
        current(
            [v4.DiagnosticRequest("parameter-read", name) for name in v4.PARAMETER_ALLOWLIST[:3]]
        )
    assert caught.value is primary
    rows = current.to_mapping()
    assert len(rows) == 2 and [r["API_returned"] for r in rows] == [True, False]
    assert rows[1]["API_attempted"] is True and "second-exact-error" in rows[1]["error"]


@pytest.mark.parametrize("primary", [KeyboardInterrupt("interrupt"), SystemExit("exit")])
def test_baseexception_is_not_replaced_and_attempt_survives(primary):
    current = sender()

    def interrupt(*_):
        raise primary

    current.connection.mav.on_call = interrupt
    with pytest.raises(type(primary)) as caught:
        current([v4.DiagnosticRequest("autopilot-version")])
    assert caught.value is primary
    assert current.to_mapping()[0]["error"].startswith(type(primary).__name__)


def test_post_api_deadline_failure_keeps_actual_return_not_false_zero():
    calls = []

    def remaining():
        calls.append(True)
        if len(calls) == 2:
            raise TimeoutError("owned deadline")
        return 1

    current = sender(remaining=remaining)
    with pytest.raises(TimeoutError):
        current([v4.DiagnosticRequest("autopilot-version")])
    row = current.to_mapping()[0]
    assert row["API_attempted"] is True and row["API_returned"] is True
    assert row["API_returned_at_s"] is None and "owned deadline" in row["error"]


def test_clock_regression_preserves_return_and_secondary_clock_error():
    times = iter([10.0, 9.0, 8.0])
    current = sender(clock=lambda: next(times))
    with pytest.raises(ValueError, match="regressed"):
        current([v4.DiagnosticRequest("autopilot-version")])
    row = current.to_mapping()[0]
    assert row["API_returned"] is True and row["API_returned_at_s"] is None
    assert "regressed" in row["error"] and "regressed" in row["secondary_error"]


def test_sender_refuses_another_thread_after_owner_claim():
    current = sender()
    current([v4.DiagnosticRequest("autopilot-version")])
    errors = []

    def foreign():
        try:
            current([v4.DiagnosticRequest("parameter-read", "BAT1_CAPACITY")])
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(1)
    assert not thread.is_alive() and len(errors) == 1
    assert "exclusive RX" in str(errors[0]) and len(current.to_mapping()) == 1


def test_unique_rx_owns_reads_and_all_sends_deferred_decode_on_main():
    current, connection, send = receiver()
    main = threading.get_ident()
    original = RawMessage()
    connection.inbox.put(original)
    current.start()
    try:
        rows = current.request_diagnostics([v4.DiagnosticRequest("autopilot-version")])
        batch = current.read_batch(1)
        assert len(batch) == 1 and original.decoded_threads == []
        message, received = batch[0]
        assert (
            message.get_type() == "PARAM_VALUE" and message.to_dict()["param_id"] == "BAT1_CAPACITY"
        )
        assert original.decoded_threads == [main, main] and received > 0
        rx = current._thread.ident
        assert rx != main and set(connection.read_threads) == {rx}
        assert {r[2] for r in connection.mav.calls} == {rx}
        assert rows[0]["API_owner_thread_id"] == rx == send._owner
    finally:
        current.stop()
    assert current.to_mapping()["thread_exited"] is True


def test_stream_compatibility_then_all_diagnostics_fit_fixed_cumulative_budget():
    current, connection, _ = receiver()
    current.start()
    try:
        stream = current.request_streams(v4.STREAM_ALLOWLIST)
        assert len(stream) == 11 and all(r["kind"] == "stream-interval" for r in stream)
        diagnostics = current.request_diagnostics(
            [v4.DiagnosticRequest("parameter-read", n) for n in v4.PARAMETER_ALLOWLIST]
            + [v4.DiagnosticRequest("autopilot-version")]
        )
        assert len(diagnostics) == 19 and len(connection.mav.calls) == 30
        assert current.to_mapping()["request_slots_reserved"] == 30
        current.request_diagnostics([v4.DiagnosticRequest("parameter-read", "SYS_AUTOSTART")])
        current.request_diagnostics([v4.DiagnosticRequest("parameter-read", "MAV_TYPE")])
        with pytest.raises(ValueError, match="budget exhausted"):
            current.request_diagnostics([v4.DiagnosticRequest("autopilot-version")])
        assert len(connection.mav.calls) == 32
    finally:
        current.stop()


def test_failure_keeps_all_reserved_slots_and_prevents_restart_or_retry():
    current, connection, send = receiver()
    primary = OSError("read-request-failed")

    def fail(*_):
        raise primary

    connection.mav.on_call = fail
    current.start()
    with pytest.raises(OSError) as caught:
        current.request_diagnostics(
            [v4.DiagnosticRequest("parameter-read", n) for n in v4.PARAMETER_ALLOWLIST[:4]]
        )
    assert caught.value is primary
    current.stop()
    assert current.to_mapping()["request_slots_reserved"] == 4
    assert len(send.to_mapping()) == 1 and len(connection.mav.calls) == 1
    with pytest.raises(RuntimeError, match="read-request-failed"):
        current.request_diagnostics([v4.DiagnosticRequest("autopilot-version")])
    with pytest.raises(RuntimeError, match="single-use"):
        current.start()


def test_missing_sender_is_failure_not_fabricated_response():
    current = v4.ConcurrentTelemetryReceiverV4(lambda _: None, time.monotonic).start()
    try:
        with pytest.raises(RuntimeError, match="No observation"):
            current.request_diagnostics([v4.DiagnosticRequest("autopilot-version")])
    finally:
        current.stop()
    assert current.to_mapping()["request_API_calls"] == 0


def test_no_parameter_response_is_not_replaced_with_default_or_success():
    current, _, send = receiver()
    current.start()
    try:
        current.request_diagnostics([v4.DiagnosticRequest("parameter-read", "BAT_AVRG_CURRENT")])
        assert current.read_batch(0.01) == []
        row = send.to_mapping()[0]
        assert row["API_returned"] is True and row["response_observed_here"] is False
        assert "parameter_value" not in row and "default" not in row
    finally:
        current.stop()


def test_stop_allows_finite_original_queue_drain_no_new_send():
    current, connection, _ = receiver()
    for _ in range(3):
        connection.inbox.put(RawMessage())
    current.start()
    wait_until(lambda: current.to_mapping()["received_packets"] == 3)
    current.stop()
    drained = current.read_batch(0)
    assert len(drained) == 3 and current.read_batch(0) == []
    report = current.to_mapping()
    assert report["received_packets"] == report["consumed_packets"] == 3
    assert report["queued_packets"] == 0 and report["thread_alive"] is False
    with pytest.raises(RuntimeError, match="Live exclusive"):
        current.request_diagnostics([v4.DiagnosticRequest("autopilot-version")])


def test_queue_overflow_remains_failclosed_not_diagnostic_success():
    current, connection, _ = receiver(capacity=1)
    connection.inbox.put(RawMessage())
    current.start()
    wait_until(lambda: current.to_mapping()["thread_exited"])
    current.stop()
    assert current.to_mapping()["queue_overflow"] is True
    assert len(current.read_batch(0)) == 1
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        current.request_diagnostics([v4.DiagnosticRequest("autopilot-version")])


def test_diagnostic_control_refuses_foreign_consumer_thread():
    current, _, _ = receiver()
    errors = []
    current.start()

    def foreign():
        try:
            current.request_diagnostics([v4.DiagnosticRequest("autopilot-version")])
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(1)
    current.stop()
    assert len(errors) == 1 and "another consumer" in str(errors[0])
    assert current.to_mapping()["request_slots_reserved"] == 0


def test_owned_session_reuses_frozen_lifecycle_and_closes_rx_on_partial_api_error(plan):
    connection, docker = Connection(), helpers.NativeDocker(plan)
    clock = HighResolutionSystemClock()
    backend = helpers.Backend(plan, clock)
    primary = OSError("diagnostic-send-original")

    def fail(*_):
        raise primary

    connection.mav.on_call = fail
    owned = v4.OwnedTeacherGazeboSessionV4(
        plan,
        execute=True,
        clock=clock,
        runner=docker,
        connection_factory=lambda *_args, **_kwargs: connection,
        camera_factory=lambda *_: backend,
        port_probe=lambda _: None,
    )
    with pytest.raises(OSError) as caught:
        with owned:
            rx = owned.start_receiver()
            rx.request_diagnostics([v4.DiagnosticRequest("autopilot-version")])
    assert caught.value is primary and connection.closed
    assert docker.current is None
    assert [r[0] for r in docker.records if r[0] in ("stop", "rm")] == ["stop", "rm"]
    report = owned.to_mapping()
    assert report["cleanup_verified"] is True and report["receiver"]["thread_alive"] is False
    assert report["diagnostic_request_API_attempts"][0]["API_returned"] is False
    assert report["stream_request_API_attempts"] == []
    assert report["flight_authority"] is False
    receipt = json.loads((plan.evidence_dir / "session-receipt.json").read_text())
    assert receipt["contract"] == "multidetect.onboard-teacher-gazebo-session-v1"
    assert receipt["status"] == "failed" and receipt["cleanup_verified"] is True
    assert report["contract"] == v4.CONTRACT


def test_plan_only_has_no_diagnostics_or_external_operations(plan):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("No actual operation allowed")

    with v4.OwnedTeacherGazeboSessionV4(
        plan,
        runner=forbidden,
        connection_factory=forbidden,
        camera_factory=forbidden,
        port_probe=forbidden,
    ) as owned:
        with pytest.raises(v4.TeacherSessionError, match="Opened owned"):
            owned.start_receiver()
    assert not plan.evidence_dir.exists()
    assert owned.to_mapping()["diagnostic_request_API_attempts"] == []


def test_import_quiet_and_frozen_v3_identity_not_changed():
    source = ROOT / "scripts/onboard_teacher_gazebo_session_v3.py"
    assert len(source.read_bytes()) == 19604
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        "04d57a9bb00eae73a355d53a6263c4054019ca6ccc7490ec2eaa9d3bdea07a22"
    )
    command = (
        "import sys;sys.path[:0]=[" + repr(str(ROOT)) + "," + repr(str(ROOT / "src")) + "];"
        "import scripts.onboard_teacher_gazebo_session_v4;"
        "blocked=('torch','transformers','peft','PIL','numpy','pymavlink');"
        "assert not any(n.split('.')[0] in blocked for n in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", command], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

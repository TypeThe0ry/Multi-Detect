"""CPU lifecycle and original-output fixtures; never launch Docker or PX4."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_teacher_gazebo_observation_v3 import ThreadClock
from test_onboard_teacher_observation_context_integration import Message, message_fields

from scripts import run_onboard_teacher_px4_diagnostics as diagnostics
from scripts.onboard_teacher_gazebo_session_v4 import (
    ConcurrentTelemetryReceiverV4,
    ReadOnlyDiagnosticSender,
)
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3

CID = "d" * 64


@pytest.fixture
def harness(tmp_path):
    manifest = tmp_path / "fixture.scenario.json"
    manifest.write_bytes(b"{}")
    runtime = tmp_path / "fixture.runtime.json"
    runtime.write_bytes(b'{"world":{"wind_north_mps":1,"wind_east_mps":-0.4}}')
    plan = SimpleNamespace(
        source_manifest_path=manifest,
        docker_plan=SimpleNamespace(docker_executable="docker-fixture"),
        to_mapping=lambda: {"CPU_fixture": True, "flight_authority": False},
    )
    events, sessions, native_calls = [], [], []
    config = SimpleNamespace(
        no_heartbeat=False,
        send_error_at=None,
        capture_error=None,
        seal_error=None,
        abort_error=None,
        stop_error=None,
        mapping_error=None,
        receiver_mapping_error=None,
        source_drift=False,
        native_error_at=None,
        cleanup_error=None,
        identity_error=False,
    )

    class Receiver(ConcurrentTelemetryReceiverV4):
        def read_batch(self, timeout_s=0.02):
            rows = super().read_batch(timeout_s)
            if self._reserved_requests == 30 and not self._stop.is_set():
                self.test_clock.advance(1.1)
            return rows

        def to_mapping(self):
            if config.receiver_mapping_error and self._stop.is_set():
                raise RuntimeError(config.receiver_mapping_error)
            return super().to_mapping()

    class Session:
        def __init__(self, selected, *, execute):
            assert selected is plan and execute is True
            self.plan, self.clock, self.container_id = plan, ThreadClock(), CID
            self.main = threading.get_ident()
            self.sent, self.receive_threads, self.sequence = [], [], 0
            self.receiver, self.closed, self.owned_inspections = None, False, 0
            self.stop_event = threading.Event()

            def send(*args):
                self.sent.append((threading.get_ident(), args))
                assert threading.get_ident() != self.main
                if config.send_error_at == len(self.sent):
                    raise RuntimeError("original request API failure")

            self.sender = ReadOnlyDiagnosticSender(
                SimpleNamespace(
                    mav=SimpleNamespace(
                        command_long_send=send,
                        param_request_read_send=send,
                    )
                ),
                self.clock.monotonic,
                self.clock.session_id,
                self._remaining,
            )
            sessions.append(self)

        def __enter__(self):
            events.append("session-enter")
            return self

        def __exit__(self, kind, body, traceback):
            if self.receiver is not None:
                self.receiver.stop()
            self.closed = True
            events.append("session-cleanup")
            if config.cleanup_error:
                if body is not None:
                    body.add_note(config.cleanup_error)
                    return False
                raise RuntimeError(config.cleanup_error)
            return False

        def _remaining(self):
            return 50.0

        def reader(self, timeout):
            self.receive_threads.append(threading.get_ident())
            self.stop_event.wait(min(timeout, 0.004))
            if config.no_heartbeat:
                return None
            fields = message_fields()["HEARTBEAT"]
            message = Message("HEARTBEAT", copy.deepcopy(fields), self.sequence)
            self.sequence += 1
            return message

        def start_receiver(self, queue_capacity=256):
            events.append("rx-start")
            self.receiver = Receiver(
                self.reader,
                self.clock.monotonic,
                request_sender=self.sender,
                queue_capacity=queue_capacity,
            )
            self.receiver.test_clock = self.clock
            return self.receiver.start()

        def stop_receiver(self):
            events.append("rx-stop")
            self.stop_event.set()
            self.receiver.stop()
            if config.stop_error:
                raise RuntimeError(config.stop_error)

        def _inspect(self, identity):
            assert identity == self.container_id
            return {"Id": identity, "CPU_fixture": True}

        def _owned(self, row):
            self.owned_inspections += 1
            if config.identity_error:
                return "e" * 64
            return row["Id"]

        def to_mapping(self):
            if config.mapping_error:
                raise RuntimeError(config.mapping_error)
            return {
                "CPU_fixture": True,
                "cleanup_verified": self.closed,
                "diagnostic_request_API_attempts": self.sender.to_mapping(),
                "parameter_write_performed": False,
                "flight_authority": False,
            }

    class Capture(TeacherTelemetryCaptureV3):
        def append_batch(self, rows):
            assert threading.get_ident() == sessions[-1].main
            events.append("journal-append")
            if config.capture_error:
                raise MemoryError(config.capture_error)
            return super().append_batch(rows)

        def seal(self):
            events.append("journal-seal")
            assert not sessions[-1].receiver.to_mapping()["thread_alive"]
            if config.seal_error:
                raise RuntimeError(config.seal_error)
            result = super().seal()
            if config.source_drift:
                runtime.write_bytes(b"{}")
            return result

        def abort(self, reason):
            events.append("journal-abort")
            if config.abort_error:
                raise RuntimeError(config.abort_error)
            return super().abort(reason)

    def native_runner(command, *, timeout_s):
        session = sessions[-1]
        assert timeout_s == 15
        assert not session.receiver.to_mapping()["thread_alive"]
        assert session.receiver.to_mapping()["queued_packets"] == 0
        assert "journal-seal" in events
        events.append("native-read")
        native_calls.append(command)
        if config.native_error_at == len(native_calls):
            raise subprocess.TimeoutExpired(command, timeout_s)
        return SimpleNamespace(returncode=0, stdout=b"CPU-native-original\x00\xff", stderr=b"")

    return SimpleNamespace(
        root=tmp_path,
        output=tmp_path / "diagnostics",
        runtime=runtime,
        plan=plan,
        events=events,
        sessions=sessions,
        native_calls=native_calls,
        config=config,
        factory=Session,
        capture=Capture,
        native_runner=native_runner,
    )


def run(harness, **kwargs):
    return diagnostics.diagnose_px4(
        harness.plan,
        harness.output,
        execute=True,
        observation_s=1,
        session_factory=harness.factory,
        capture_factory=harness.capture,
        native_runner=harness.native_runner,
        artifact_root=harness.root,
        **kwargs,
    )


def assert_persisted(harness, report):
    raw = Path(report["receipt"]["path"]).read_bytes()
    assert len(raw) == report["receipt"]["bytes"]
    assert hashlib.sha256(raw).hexdigest() == report["receipt"]["sha256"]
    persisted = json.loads(raw)
    assert persisted["status"] == report["status"]
    assert persisted["error"] == report["error"]
    assert not any(persisted["claims"].values())
    return persisted


def test_default_plan_only_no_session_capture_native_or_output(harness):
    def denied(*args, **kwargs):
        raise AssertionError("A plan-only call executed an effect")

    report = diagnostics.diagnose_px4(
        harness.plan,
        harness.output,
        session_factory=denied,
        capture_factory=denied,
        native_runner=denied,
        artifact_root=harness.root,
    )
    assert report["status"] == "planned" and report["execute_requested"] is False
    assert not harness.output.exists() and harness.events == []
    assert report["external_SIM_observed_here"] is False
    assert report["raw_telemetry"] is None and report["native_reads"] == []


@pytest.mark.parametrize("value", [0, 1, None, "yes"])
def test_execute_is_strict_before_effects(harness, value):
    with pytest.raises(TypeError):
        diagnostics.diagnose_px4(
            harness.plan,
            harness.output,
            execute=value,
            artifact_root=harness.root,
        )
    assert not harness.output.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("observation_s", 0),
        ("observation_s", 31),
        ("observation_s", True),
        ("queue_capacity", 15),
        ("queue_capacity", 4097),
        ("queue_capacity", True),
    ],
)
def test_bounded_inputs_before_effects(harness, field, value):
    with pytest.raises((TypeError, ValueError)):
        diagnostics.diagnose_px4(
            harness.plan,
            harness.output,
            execute=True,
            artifact_root=harness.root,
            **{field: value},
        )
    assert not harness.output.exists()


def test_existing_or_escaping_outputs_not_overwritten(harness):
    harness.output.mkdir()
    sentinel = harness.output / "original"
    sentinel.write_bytes(b"keep")
    with pytest.raises(ValueError):
        run(harness)
    with pytest.raises(ValueError):
        diagnostics.diagnose_px4(
            harness.plan,
            harness.root.parent / "escape",
            artifact_root=harness.root,
        )
    assert sentinel.read_bytes() == b"keep" and harness.events == []


def test_one_real_rx_sender_and_main_journal_close_before_native(harness):
    report = run(harness)
    assert report["status"] == "read-only-diagnostics-recorded", report
    assert report["injected_dependencies_used"] is True
    assert report["external_SIM_observed_here"] is False
    assert report["context_qualified"] is report["closed_snapshot_is_live_context"] is False
    session = harness.sessions[0]
    assert len(harness.sessions) == 1 and len(session.sent) == 30
    assert {thread for thread, _ in session.sent} == set(session.receive_threads)
    assert set(session.receive_threads) != {session.main}
    assert harness.events.index("rx-stop") < harness.events.index("journal-seal")
    assert harness.events.index("journal-seal") < harness.events.index("native-read")
    assert report["receiver"]["thread_alive"] is False
    assert report["receiver"]["queued_packets"] == 0
    assert report["raw_telemetry"]["status"] == "recorded"
    assert report["raw_telemetry"]["orphan_packet_count"] == 0
    assert len(report["native_reads"]) == 20
    assert len(report["request_rows"]) == 30
    assert all(not row["flight_authority"] for row in report["request_rows"])
    assert all(not row["response_observed_here"] for row in report["request_rows"])
    for command in harness.native_calls:
        assert command[0] == "docker-fixture"
        if command[1] == "logs":
            assert command == ["docker-fixture", "logs", "--tail", "200", CID]
        else:
            assert command[1:3] == ["exec", CID]
            assert command[3:5] in (
                ["/opt/px4-gazebo/bin/px4-ver", "all"],
                ["/opt/px4-gazebo/bin/px4-param", "show"],
            )
    assert all("injected" in row["source"] for row in report["native_reads"])
    assert_persisted(harness, report)


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("capture_error", "original journal failure", "MemoryError"),
        ("seal_error", "original seal failure", "original seal failure"),
        ("stop_error", "original stop failure", "original stop failure"),
        ("source_drift", True, "source drifted"),
    ],
)
def test_primary_failures_abort_no_native_success(harness, key, value, expected):
    setattr(harness.config, key, value)
    report = run(harness)
    assert report["status"] == "diagnostics-failed"
    assert expected in report["error"]
    assert harness.native_calls == []
    assert report["external_SIM_observed_here"] is False
    assert harness.sessions[0].closed
    assert_persisted(harness, report)


def test_original_capture_failure_survives_abort_and_mapping_errors(harness):
    harness.config.capture_error = "primary journal error"
    harness.config.abort_error = "secondary abort error"
    harness.config.mapping_error = "secondary session report error"
    harness.config.receiver_mapping_error = "secondary receiver report error"
    report = run(harness)
    assert "primary journal error" in report["error"]
    assert any("secondary abort error" in error for error in report["close_errors"])
    assert any("secondary session report error" in error for error in report["close_errors"])
    assert any("secondary receiver report error" in error for error in report["close_errors"])
    assert harness.sessions[0].closed and harness.native_calls == []
    assert_persisted(harness, report)


def test_partial_request_api_attempts_remain_in_owned_report(harness):
    harness.config.send_error_at = 13
    report = run(harness)
    assert report["status"] == "diagnostics-failed"
    attempts = report["session"]["diagnostic_request_API_attempts"]
    assert len(attempts) == 13
    assert sum(row["API_returned"] for row in attempts) == 12
    assert attempts[-1]["API_returned"] is False
    assert "original request API failure" in attempts[-1]["error"]
    assert report["receiver"]["thread_alive"] is False
    assert harness.native_calls == []
    assert_persisted(harness, report)


def test_missing_durable_heartbeat_never_requests_or_reads_native(harness):
    harness.config.no_heartbeat = True
    report = run(harness)
    assert report["status"] == "diagnostics-failed"
    assert "source-bound heartbeat" in report["error"]
    assert harness.sessions[0].sent == [] and report["request_rows"] == []
    assert harness.native_calls == []
    assert report["receiver"]["thread_alive"] is False
    assert report["raw_telemetry"]["status"] == "aborted"
    assert_persisted(harness, report)


def test_later_native_failure_preserves_prior_files_and_failed_attempt(harness):
    harness.config.native_error_at = 3
    report = run(harness)
    assert report["status"] == "diagnostics-failed" and "TimeoutExpired" in report["error"]
    assert len(report["native_reads"]) == 3
    assert report["native_reads"][-1]["native_exit_code"] is None
    assert "TimeoutExpired" in report["native_reads"][-1]["error"]
    for row in report["native_reads"][:2]:
        assert row["native_exit_code"] == 0
        for name in ("stdout", "stderr"):
            binding = row[name]
            raw = Path(binding["path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    assert report["session"]["cleanup_verified"] is True
    assert_persisted(harness, report)


def test_native_container_identity_mismatch_before_command(harness):
    harness.config.identity_error = True
    report = run(harness)
    assert report["status"] == "diagnostics-failed"
    assert "identity changed" in report["error"] and harness.native_calls == []
    assert_persisted(harness, report)


def test_cleanup_failure_cannot_leave_success_status(harness):
    harness.config.cleanup_error = "owned cleanup failure"
    report = run(harness)
    assert report["status"] == "diagnostics-failed"
    assert "owned cleanup failure" in report["error"]
    assert_persisted(harness, report)


def native_session(tmp_path):
    return SimpleNamespace(
        container_id=CID,
        plan=SimpleNamespace(docker_plan=SimpleNamespace(docker_executable="docker-fixture")),
        clock=ThreadClock(),
        _remaining=lambda: 40.0,
        _inspect=lambda cid: {"Id": cid},
        _owned=lambda row: row["Id"],
    )


def test_nonzero_missing_parameter_kept_not_defaulted(tmp_path):
    session = native_session(tmp_path)

    def runner(command, *, timeout_s):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"Parameter not found\n")

    rows = diagnostics._native_reads(session, tmp_path, runner=runner)
    assert len(rows) == 20
    assert all(row["native_exit_code"] == 1 for row in rows)
    assert all(Path(row["stdout"]["path"]).read_bytes() == b"" for row in rows)
    assert all("value" not in row for row in rows)


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(returncode=True, stdout=b"", stderr=b""),
        SimpleNamespace(returncode=None, stdout=b"", stderr=b""),
        SimpleNamespace(returncode=0, stdout="", stderr=b""),
        SimpleNamespace(returncode=0, stdout=None, stderr=b""),
        SimpleNamespace(returncode=0, stdout=b"x" * (256 * 1024 + 1), stderr=b""),
    ],
)
def test_native_raw_exit_output_gates_keep_failed_attempt(tmp_path, result):
    rows = []
    with pytest.raises((TypeError, RuntimeError, ValueError)):
        diagnostics._native_reads(
            native_session(tmp_path),
            tmp_path,
            runner=lambda *args, **kwargs: result,
            result_rows=rows,
        )
    assert len(rows) == 1 and rows[0]["error"]
    assert list(tmp_path.iterdir()) == []


def test_stdlib_import_does_not_load_optional_packages_or_docker():
    root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys;sys.path[:0]=[{str(root)!r},{str(root / 'src')!r}];"
        "from scripts import run_onboard_teacher_px4_diagnostics;"
        "assert not any(n in sys.modules for n in "
        "['torch','transformers','PIL','pymavlink','pyarrow','datasets'])"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr

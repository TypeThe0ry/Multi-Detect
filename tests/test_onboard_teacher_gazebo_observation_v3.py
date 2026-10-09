"""V3 RX/journal/context integration with CPU stand-ins, never external SIM."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_teacher_observation_context_integration import (
    CameraObservation,
    Message,
    message_fields,
)
from test_onboard_teacher_observation_context_integration import integration as integration

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import canonical_json
from scripts.onboard_teacher_observed_context import (
    REQUIRED_MESSAGES,
    build_observed_teacher_context,
)
from scripts.onboard_teacher_telemetry_capture_v2 import TAG, decode_typed_value
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3

PROFILE_HASH = "5bd121275d285e27adef2a8f4b4242b481dffb8368ec4dd231089eeac8732d47"


class ThreadClock:
    """Thread-safe CPU test clock; no source-clock/UTC equivalence claim."""

    session_id = "v3-CPU-fixture-observer-clock"

    def __init__(self):
        self.started = time.perf_counter()
        self.offset = 0.0
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return 10.0 + time.perf_counter() - self.started + self.offset

    def advance(self, seconds):
        with self.lock:
            self.offset += seconds

    def utc_now(self):
        return datetime(2026, 10, 8, tzinfo=timezone.utc) + timedelta(seconds=self.monotonic())

    def identity(self):
        return {"session_id": self.session_id, "source": "CPU-test-clock-not-SIM-QPC"}


def read_bound(binding):
    raw = Path(binding["path"]).read_bytes()
    assert len(raw) == binding["bytes"]
    assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    return json.loads(raw)


def source(root, name, value):
    path = root / name
    raw = (canonical_json(value) + "\n").encode()
    path.write_bytes(raw)
    return EvidenceBinding(name, len(raw), hashlib.sha256(raw).hexdigest(), name, "")


@pytest.fixture
def journal_context(tmp_path):
    clock = ThreadClock()
    capture = TeacherTelemetryCaptureV3(
        tmp_path / "journal",
        clock=clock.monotonic,
        clock_id=clock.session_id,
        transaction_id="CPU-parent-observation-only",
        local_origin_id="unresolved-CPU-EKF-origin",
    )
    fields = message_fields()
    return SimpleNamespace(root=tmp_path, clock=clock, capture=capture, fields=fields)


def append_required(harness):
    samples = [
        (Message(kind, copy.deepcopy(harness.fields[kind]), index), harness.clock.monotonic())
        for index, kind in enumerate(REQUIRED_MESSAGES)
    ]
    return harness.capture.append_batch(samples)


def materialize_and_qualify(harness, *, name="snapshot"):
    clock = harness.clock
    now = clock.monotonic()
    rows = harness.capture.materialize_snapshot(
        list(REQUIRED_MESSAGES), harness.root / name, now_s=now, maximum_age_s=2
    )
    profile = source(
        harness.root,
        f"{name}-profile.json",
        {
            "schema_version": 1,
            "airframe_profile_id": "px4_multirotor_gazebo_v1",
            "vehicle_model": "px4_multirotor_standard",
            "model_name": "x500_mono_cam",
            "stack_plan_sha256": "a" * 64,
        },
    )
    resources = source(
        harness.root,
        f"{name}-resources.json",
        {
            "clock_id": clock.session_id,
            "transaction_id": harness.capture.transaction_id,
            "observed_at_s": now,
            "airframe_profile_hash": PROFILE_HASH,
            "geofence_valid": False,
            "camera_available": False,
            "perception_available": False,
            "map_cache_available": False,
            "dem_available": False,
            "wind_north_mps": 1.0,
            "wind_east_mps": -0.4,
            "wind_source": "software-SIM-configured",
        },
    )
    return rows, build_observed_teacher_context(
        rows,
        artifact_root=harness.root,
        now_s=clock.monotonic(),
        captured_at_utc=clock.utc_now().isoformat(),
        clock_id=clock.session_id,
        transaction_id=harness.capture.transaction_id,
        local_origin_id=harness.capture.local_origin_id,
        profile_source=profile,
        profile_id="px4_multirotor_gazebo_v1",
        resource_source=resources,
    )


def test_actual_v3_journal_materialization_to_frozen_context(journal_context):
    original = append_required(journal_context)
    rows, context = materialize_and_qualify(journal_context)
    assert [row["received_at_s"] for row in rows.values()] == [
        row["received_at_s"] for row in original
    ]
    assert len(context.source_references) == 20
    assert context.context.estimated_flight_time_s > 1198
    assert context.context.geofence_valid is context.context.perception_available is False
    assert all(value is False for value in context.to_mapping()["claims"].values())
    receipt = journal_context.capture.seal()
    assert receipt["status"] == "recorded" and receipt["record_count"] == 9
    assert receipt["orphan_packet_count"] == 0
    assert receipt["raw_commit_before_decoded_access"] is True
    assert receipt["dual_commit_before_cache"] is True
    assert all(value is False for value in receipt["claims"].values())


@pytest.mark.parametrize("remaining", [0, -1])
def test_unknown_endurance_is_durable_but_cannot_qualify(journal_context, remaining):
    journal_context.fields["BATTERY_STATUS"]["time_remaining"] = remaining
    append_required(journal_context)
    with pytest.raises(ValueError, match="time_remaining|reported estimate"):
        materialize_and_qualify(journal_context)
    row = journal_context.capture.latest_message(
        "BATTERY_STATUS", now_s=journal_context.clock.monotonic(), maximum_age_s=2
    )
    assert row["message"]["time_remaining"] == remaining
    assert journal_context.capture.seal()["status"] == "recorded"


def test_MC_airspeed_nan_stays_tagged_and_actual_context_refuses(journal_context):
    journal_context.fields["VFR_HUD"]["airspeed"] = math.nan
    append_required(journal_context)
    with pytest.raises((ValueError, TypeError), match="airspeed"):
        materialize_and_qualify(journal_context)
    row = journal_context.capture.latest_message(
        "VFR_HUD", now_s=journal_context.clock.monotonic(), maximum_age_s=2
    )
    tagged = row["message"]["airspeed"]
    assert set(tagged) == {TAG} and math.isnan(decode_typed_value(tagged[TAG]))
    assert journal_context.capture.seal()["status"] == "recorded"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_required_accuracy_preserved_and_context_refused(journal_context, value):
    journal_context.fields["GPS_RAW_INT"]["h_acc"] = value
    append_required(journal_context)
    with pytest.raises((ValueError, TypeError)):
        materialize_and_qualify(journal_context)
    row = journal_context.capture.latest_message(
        "GPS_RAW_INT", now_s=journal_context.clock.monotonic(), maximum_age_s=2
    )
    tagged = row["message"]["h_acc"]
    assert set(tagged) == {TAG}
    actual = decode_typed_value(tagged[TAG])
    assert math.isnan(actual) if math.isnan(value) else actual == value
    journal_context.capture.seal()


def test_repeated_snapshot_cannot_renew_actual_receive_time(journal_context):
    original = append_required(journal_context)
    materialize_and_qualify(journal_context)
    journal_context.clock.advance(2.01)
    for kind in REQUIRED_MESSAGES:
        assert (
            journal_context.capture.latest_message(
                kind, now_s=journal_context.clock.monotonic(), maximum_age_s=2
            )
            is None
        )
    with pytest.raises(ValueError, match="missing/stale/future"):
        journal_context.capture.materialize_snapshot(
            list(REQUIRED_MESSAGES),
            journal_context.root / "not-renewed",
            now_s=journal_context.clock.monotonic(),
        )
    receipt = journal_context.capture.abort("CPU stale-snapshot refusal")
    assert receipt["status"] == "aborted"
    assert [row["received_at_s"] for row in receipt["records"]] == [
        row["received_at_s"] for row in original
    ]


def test_actual_standby_status_not_repaired_to_pass(journal_context):
    journal_context.fields["HEARTBEAT"]["system_status"] = 0
    append_required(journal_context)
    with pytest.raises(ValueError, match="actual STANDBY"):
        materialize_and_qualify(journal_context)
    assert journal_context.capture.seal()["record_count"] == 9


def test_foreign_binary_is_preserved_but_not_a_context_candidate(journal_context):
    class Foreign(Message):
        def get_srcSystem(self):
            return 0

        def get_srcComponent(self):
            return 0

    journal_context.capture.append_batch(
        [
            (
                Foreign("UNKNOWN_411", {"data": bytearray(b"\x00\xff")}, 0),
                journal_context.clock.monotonic(),
            )
        ]
    )
    append_required(journal_context)
    rows, _ = materialize_and_qualify(journal_context)
    assert all(row["source_accepted"] is True for row in rows.values())
    assert "UNKNOWN_411" not in rows
    receipt = journal_context.capture.seal()
    assert receipt["record_count"] == 10 and receipt["foreign_source_count"] == 1


def test_receiver_continues_while_caller_blocks_on_camera():
    api = importlib.import_module("scripts.onboard_teacher_gazebo_session_v3")
    calls, second, released = [], threading.Event(), threading.Event()

    def reader(timeout):
        calls.append(threading.get_ident())
        if len(calls) == 2:
            second.set()
        released.wait(min(timeout, 0.01))
        return None

    rx = api.ConcurrentTelemetryReceiver(reader, ThreadClock().monotonic, queue_capacity=8)
    rx.start()
    try:
        assert second.wait(2), "RX must continue while main camera/persist owner is blocked"
        assert set(calls) != {threading.get_ident()}
        rx.raise_if_failed()
    finally:
        released.set()
        rx.stop()


@pytest.mark.parametrize("capacity", [True, 0, -1])
def test_receiver_queue_capacity_is_strict_and_bounded(capacity):
    api = importlib.import_module("scripts.onboard_teacher_gazebo_session_v3")
    with pytest.raises((TypeError, ValueError)):
        api.ConcurrentTelemetryReceiver(
            lambda timeout: None, ThreadClock().monotonic, queue_capacity=capacity
        )


@pytest.fixture
def threaded_fixture(integration, tmp_path):
    """Real receiver/journal/context; only connection/camera/ownership are stand-ins."""
    api = importlib.import_module("scripts.onboard_teacher_gazebo_session_v3")
    plan, fields, _, _ = integration
    sessions = []
    configuration = SimpleNamespace(
        block_camera=None,
        seal_error=None,
        fail_reader_after=None,
        fast_reader=False,
        camera_error=None,
        before_persist=None,
    )

    class Session:
        def __init__(self, given_plan, *, execute):
            assert given_plan is plan and execute is True
            self.clock = ThreadClock()
            self.main_thread = threading.get_ident()
            self.reader_calls, self.sent, self.updates = [], [], []
            self.reader_condition = threading.Condition()
            self.update_lock = threading.Lock()
            self.closed, self.sequence, self.receiver = False, 0, None
            self.camera_thread = None
            self.camera_stop = threading.Event()
            self.camera_done = threading.Event()
            self.camera_fail = None
            self.camera_started = threading.Event()
            sessions.append(self)

        def reader(self, timeout):
            with self.reader_condition:
                self.reader_calls.append(threading.get_ident())
                self.reader_condition.notify_all()
            if configuration.fail_reader_after is not None:
                if self.sequence >= configuration.fail_reader_after:
                    raise RuntimeError("CPU original RX failure")
            if not configuration.fast_reader:
                self.camera_stop.wait(min(timeout, 0.004))
            kind = REQUIRED_MESSAGES[self.sequence % len(REQUIRED_MESSAGES)]
            message = Message(kind, copy.deepcopy(fields[kind]), self.sequence)
            self.sequence += 1
            return message

        def request_sender(self, names):
            assert threading.get_ident() != self.main_thread
            self.sent.append((tuple(names), threading.get_ident()))
            return [
                {
                    "message_type": name,
                    "args": [1, 1, 511, 0, index, 250000, 0, 0, 0, 0, 0],
                    "clock_id": self.clock.session_id,
                    "API_called_at_s": self.clock.monotonic(),
                    "acknowledged": False,
                    "datagram_delivery_independently_verified": False,
                }
                for index, name in enumerate(names)
            ]

        def start_receiver(self, queue_capacity=256):
            self.receiver = api.ConcurrentTelemetryReceiver(
                self.reader,
                self.clock.monotonic,
                request_sender=self.request_sender,
                queue_capacity=queue_capacity,
            )
            self.receiver.start()
            return self.receiver

        def start_camera_windows(self, directory, persist_callback, *, windows=3, interval_s=1):
            def run_camera():
                try:
                    self.camera_started.set()
                    if configuration.block_camera is not None:
                        assert configuration.block_camera.wait(2)
                    for window in range(windows):
                        if self.camera_stop.is_set():
                            return
                        for index in range(plan.frame_count):
                            if configuration.camera_error is not None:
                                raise configuration.camera_error
                            observation = CameraObservation(self.clock)
                            path = Path(directory) / f"cpu-window-{window:03d}-{index:03d}"
                            path.mkdir()
                            if configuration.before_persist is not None:
                                configuration.before_persist(self)
                            binding = persist_callback(path, observation)
                            with self.update_lock:
                                self.updates.append(
                                    {
                                        "camera": binding,
                                        "host_return_at_s": observation.host_return_stamp.value_ns
                                        / 1e9,
                                        "window_index": window,
                                        "frame_index": index,
                                    }
                                )
                        self.camera_stop.wait(min(interval_s, 0.01))
                except BaseException as error:
                    self.camera_fail = error
                finally:
                    self.camera_done.set()

            self.camera_thread = threading.Thread(target=run_camera, name="CPU-v3-camera")
            self.camera_thread.start()

        def camera_updates(self):
            if self.camera_fail is not None:
                raise self.camera_fail
            with self.update_lock:
                rows, self.updates = self.updates, []
                return rows

        def camera_finished(self):
            return self.camera_done.is_set()

        def stop_camera(self):
            self.camera_stop.set()
            if self.camera_thread is not None:
                self.camera_thread.join(2)
                assert not self.camera_thread.is_alive()

        def stop_receiver(self):
            if self.receiver is not None:
                self.receiver.stop()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stop_camera()
            self.stop_receiver()
            self.closed = True
            return False

        def attest_qualified(self, *, telemetry_qualified):
            assert telemetry_qualified is True
            return SimpleNamespace(to_mapping=lambda: {"CPU_fixture": True, "not_authority": True})

        def to_mapping(self):
            return {
                "CPU_fixture": True,
                "closed": self.closed,
                "cleanup_verified": self.closed,
                "camera_windows": {"closed": self.camera_done.is_set()},
            }

    return SimpleNamespace(
        root=tmp_path,
        plan=plan,
        fields=fields,
        factory=Session,
        sessions=sessions,
        config=configuration,
    )


def execute_threaded(harness, **kwargs):
    runner = importlib.import_module("scripts.run_onboard_teacher_gazebo_observation_v3")
    return runner.observe_teacher(
        harness.plan,
        harness.root / "observation-v3",
        execute=True,
        context_timeout_s=1,
        session_factory=harness.factory,
        artifact_root=harness.root,
        camera_windows=2,
        camera_interval_s=0.01,
        **kwargs,
    )


def assert_closed_false_claims(report, harness):
    assert harness.sessions[0].closed is True
    assert report["session"]["closed"] is True
    assert all(value is False for value in report["claims"].values())
    assert report["external_SIM_observed_here"] is False
    assert report["injected_dependencies_used"] is True


def test_threaded_actual_journal_context_positive_and_all_claims_false(threaded_fixture):
    report = execute_threaded(threaded_fixture)
    assert report["status"] == "observed-context-qualified", report
    assert report["context_qualified"] is True
    assert report["raw_telemetry"]["status"] == "recorded"
    assert report["raw_telemetry"]["accepted_source_count"] >= 9
    assert report["telemetry_stream_request_API_calls"] == 11
    assert report["camera_frames_recorded"] == 4
    frames = report["camera_frames"]
    assert [(row["window_index"], row["frame_index"]) for row in frames] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    ]
    assert len({row["camera"]["path"] for row in frames}) == 4
    times = [row["host_return_at_s"] for row in frames]
    assert all(later > earlier for earlier, later in zip(times[:-1], times[1:], strict=True))
    context = read_bound(report["context"])
    assert context["context"]["estimated_flight_time_s"] > 1198
    assert all(value is False for value in context["claims"].values())
    session = threaded_fixture.sessions[0]
    assert len(set(session.reader_calls)) == 1
    assert session.reader_calls[0] != session.main_thread
    assert session.sent[0][1] == session.reader_calls[0]
    assert_closed_false_claims(report, threaded_fixture)


@pytest.mark.parametrize("change", ["unknown-endurance", "MC-NaN", "not-standby"])
def test_threaded_refused_context_preserves_actual_bad_fields(threaded_fixture, change):
    if change == "unknown-endurance":
        threaded_fixture.fields["BATTERY_STATUS"]["time_remaining"] = 0
    elif change == "MC-NaN":
        threaded_fixture.fields["VFR_HUD"]["airspeed"] = math.nan
    else:
        threaded_fixture.fields["HEARTBEAT"]["system_status"] = 0
    report = execute_threaded(threaded_fixture)
    assert report["status"] == "context-refused", report
    assert report["context_qualified"] is False and "context" not in report
    assert report["raw_telemetry"]["status"] == "recorded"
    assert not list((threaded_fixture.root / "observation-v3").glob("observed-context-*.json"))
    assert_closed_false_claims(report, threaded_fixture)


def test_queue_overload_failclosed_not_partial_context_success(threaded_fixture):
    threaded_fixture.config.fast_reader = True
    report = execute_threaded(threaded_fixture, queue_capacity=1)
    assert report["status"] == "observation-failed", report
    assert report["context_qualified"] is False and "context" not in report
    assert "queue" in (report["error"] or "").lower()
    assert_closed_false_claims(report, threaded_fixture)


def test_RX_error_poison_preserves_cleanup_and_cannot_qualify(threaded_fixture):
    threaded_fixture.config.fail_reader_after = 1
    report = execute_threaded(threaded_fixture)
    assert report["status"] == "observation-failed", report
    assert report["context_qualified"] is False
    assert "CPU original RX failure" in report["error"]
    assert_closed_false_claims(report, threaded_fixture)


def test_camera_error_does_not_leave_RX_or_success_claim(threaded_fixture):
    threaded_fixture.config.camera_error = RuntimeError("CPU camera/persist failed")
    report = execute_threaded(threaded_fixture)
    assert report["status"] == "observation-failed", report
    assert "CPU camera/persist failed" in report["error"]
    assert report["context_qualified"] is False
    assert_closed_false_claims(report, threaded_fixture)


def test_V3_plan_only_never_creates_RX_camera_or_journal(threaded_fixture):
    runner = importlib.import_module("scripts.run_onboard_teacher_gazebo_observation_v3")

    def forbidden(*args, **kwargs):
        raise AssertionError("Plan only cannot instantiate a session or capture")

    directory = threaded_fixture.root / "plan-only-v3"
    report = runner.observe_teacher(
        threaded_fixture.plan,
        directory,
        session_factory=forbidden,
        capture_factory=forbidden,
        artifact_root=threaded_fixture.root,
    )
    assert report["status"] == "planned"
    assert not directory.exists()
    assert report["context_qualified"] is False
    assert all(value is False for value in report["claims"].values())


def test_RX_continues_during_slow_main_journal_commit(threaded_fixture):
    progress = []

    class SlowJournal(TeacherTelemetryCaptureV3):
        def append_batch(self, samples):
            session = threaded_fixture.sessions[0]
            state = session.receiver.to_mapping()
            if state["stopped"]:
                assert state["thread_exited"] is True and state["thread_alive"] is False
                return super().append_batch(samples)  # finite post-join queue drain
            with session.reader_condition:
                before = len(session.reader_calls)
                advanced = session.reader_condition.wait_for(
                    lambda: len(session.reader_calls) >= before + 2, timeout=2
                )
            assert advanced, "RX must not wait for main-thread journal fsync/materialization"
            progress.append((before, len(session.reader_calls)))
            return super().append_batch(samples)

    report = execute_threaded(threaded_fixture, capture_factory=SlowJournal)
    assert report["status"] == "observed-context-qualified", report
    assert progress and all(after >= before + 2 for before, after in progress)
    assert_closed_false_claims(report, threaded_fixture)


def test_RX_continues_during_independent_camera_persist(threaded_fixture):
    progress = []

    def slow_persist(session):
        with session.reader_condition:
            before = len(session.reader_calls)
            advanced = session.reader_condition.wait_for(
                lambda: len(session.reader_calls) >= before + 2, timeout=2
            )
        assert advanced, "RX must continue throughout original-camera persistence"
        progress.append((before, len(session.reader_calls)))

    threaded_fixture.config.before_persist = slow_persist
    report = execute_threaded(threaded_fixture)
    assert report["status"] == "observed-context-qualified", report
    assert progress and all(after >= before + 2 for before, after in progress)
    assert_closed_false_claims(report, threaded_fixture)


@pytest.mark.parametrize("abort_fails", [False, True])
def test_seal_failure_remains_failure_with_owned_cleanup(threaded_fixture, abort_fails):
    class SealFailed(TeacherTelemetryCaptureV3):
        def seal(self):
            raise RuntimeError("CPU primary seal failure")

        def abort(self, reason):
            if abort_fails:
                super().abort(reason)
                raise OSError("CPU secondary abort failure")
            return super().abort(reason)

    report = execute_threaded(threaded_fixture, capture_factory=SealFailed)
    assert report["status"] == "observation-failed", report
    assert "CPU primary seal failure" in report["error"]
    if abort_fails:
        assert "CPU secondary abort failure" in report["capture_close_error"]
    else:
        assert report["raw_telemetry"]["status"] == "aborted"
    assert report["context_qualified"] is False
    assert_closed_false_claims(report, threaded_fixture)


def test_existing_output_is_create_only_before_session(threaded_fixture):
    directory = threaded_fixture.root / "observation-v3"
    directory.mkdir()
    (directory / "keep").write_text("caller-owned", encoding="utf-8")
    with pytest.raises((FileExistsError, ValueError)):
        execute_threaded(threaded_fixture)
    assert threaded_fixture.sessions == []
    assert (directory / "keep").read_text() == "caller-owned"


def test_late_original_runtime_drift_refuses_after_actual_context(threaded_fixture):
    calls = []

    def drifting_builder(*args, **kwargs):
        result = build_observed_teacher_context(*args, **kwargs)
        calls.append(result)
        runtime = threaded_fixture.plan.source_manifest_path.with_name("fixture.runtime.json")
        runtime.write_text(
            json.dumps({"world": {"wind_north_mps": 99.0, "wind_east_mps": -0.4}}),
            encoding="utf-8",
        )
        return result

    report = execute_threaded(threaded_fixture, context_builder=drifting_builder)
    assert calls, "Exercise post-qualification source gate, not an earlier missing-input refusal"
    assert report["status"] == "observation-failed"
    assert "Original runtime source drifted" in report["error"]
    assert report["context_qualified"] is False
    assert_closed_false_claims(report, threaded_fixture)


def test_guarded_standalone_imports_do_not_load_ML_or_open_runtime():
    repository = Path(__file__).resolve().parents[1]
    session_path = repository / "scripts/onboard_teacher_gazebo_session_v3.py"
    observer_path = repository / "scripts/run_onboard_teacher_gazebo_observation_v3.py"
    program = (
        "import sys,runpy;"
        f"sys.path[:0]=[{str(repository)!r},{str(repository / 'src')!r}];"
        f"runpy.run_path({str(session_path)!r});"
        f"runpy.run_path({str(observer_path)!r});"
        "forbidden={'torch','transformers','peft','numpy','PIL','pymavlink','pyarrow'};"
        "assert not {n.split('.')[0] for n in sys.modules}&forbidden;"
        "print('V3 import quiet; no runtime execution')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", program],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "V3 import quiet; no runtime execution"

"""Real V3 journals / bounded RX with CPU stand-ins, not external SIM evidence."""

import copy
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_teacher_gazebo_observation_v3 import ThreadClock
from test_onboard_teacher_observation_context_integration import (
    CameraObservation,
    Message,
    message_fields,
)
from test_probe_onboard_teacher_gazebo_navref import native_result

from scripts import collect_onboard_teacher_gazebo_origin_window as window
from scripts.onboard_teacher_gazebo_session_v4 import ConcurrentTelemetryReceiverV4
from scripts.onboard_teacher_sim_operator import SourceBinding


@pytest.fixture
def h(tmp_path):
    scenario = tmp_path / "fixture.scenario.json"
    scenario.write_bytes(b"{}")
    runtime = tmp_path / "fixture.runtime.json"
    runtime.write_bytes(b'{"world":{"wind_north_mps":0,"wind_east_mps":0}}')
    world = tmp_path / "world.sdf"
    world.write_bytes(b"<sdf />")
    plan = SimpleNamespace(
        source_manifest_path=scenario,
        frame_count=2,
        docker_plan=SimpleNamespace(
            docker_executable="CPU-not-Docker",
            world_path=world,
            airframe_profile_id="px4_multirotor_gazebo_v1",
            vehicle_model="px4_multirotor_standard",
            model_name="x500_mono_cam",
            stack_plan_sha256="a" * 64,
        ),
        to_mapping=lambda: {"CPU_fixture": True, "flight_authority": False},
    )
    events, sessions, calls, receive_threads, native_threads = [], [], [], [], []
    config = SimpleNamespace(
        native_error=None,
        camera_error=None,
        cleanup_error=None,
        no_heartbeat=False,
        missing_kind=None,
        missing_snapshot=False,
        duplicate_stamp=False,
        drift_parameter=False,
        source_drift=False,
        owner_changed=False,
        malformed_native=None,
        primary_error=None,
        camera_count=4,
        foreign=False,
    )
    native_block, camera_block, native_entered, camera_entered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    native_block.set()
    camera_block.set()
    fields = message_fields()
    fields.update(
        {
            "GPS_GLOBAL_ORIGIN": {
                "latitude": 13000000,
                "longitude": 1038000000,
                "altitude": 18000,
                "time_usec": 12345000,
            },
            "ATTITUDE": {"time_boot_ms": 12345, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
            "ATTITUDE_QUATERNION": {
                "time_boot_ms": 12345,
                "q1": 1.0,
                "q2": 0.0,
                "q3": 0.0,
                "q4": 0.0,
            },
        }
    )
    kinds = list(fields)

    class Session:
        def __init__(self, selected, *, execute):
            assert selected is plan and execute is True
            self.plan, self.clock, self.container_id = selected, ThreadClock(), "d" * 64
            self.deadline_s = self.clock.monotonic() + 240
            self.receiver, self.closed, self.sequence = None, False, 0
            self.main, self.camera_thread = threading.get_ident(), None
            self.camera_done, self.camera_stop = threading.Event(), threading.Event()
            self.camera_updates_queue = []
            self.lock = threading.Lock()
            self.failure = None
            sessions.append(self)

        def __enter__(self):
            events.append("owned-enter")
            return self

        def __exit__(self, kind, error, traceback):
            self.stop_receiver()
            self.stop_camera()
            self.closed = True
            events.append("owned-cleanup")
            if config.cleanup_error:
                raise RuntimeError(config.cleanup_error)
            return False

        def reader(self, timeout):
            receive_threads.append(threading.get_ident())
            time.sleep(0.002)
            if config.no_heartbeat:
                return None
            kind = kinds[self.sequence % len(kinds)]
            self.sequence += 1
            if kind == config.missing_kind:
                return None
            result = Message(kind, copy.deepcopy(fields[kind]), self.sequence)
            if config.foreign:
                result.get_srcSystem = lambda: 2
            return result

        def start_receiver(self, queue_capacity):
            events.append("RX-start")
            self.receiver = ConcurrentTelemetryReceiverV4(
                self.reader,
                self.clock.monotonic,
                queue_capacity=queue_capacity,
                request_sender=lambda names: [{"CPU_request": name.to_mapping()} for name in names],
            ).start()
            return self.receiver

        def stop_receiver(self):
            if self.receiver is not None:
                self.receiver.stop()
                events.append("RX-stopped")

        def start_camera_windows(self, directory, persist, *, windows, interval_s):
            assert threading.get_ident() == self.main and windows == 2 and interval_s == 2
            assert "before-listener" in events
            events.append("camera-start")

            def acquire():
                try:
                    camera_entered.set()
                    if not camera_block.wait(2):
                        raise RuntimeError("CPU camera hold timed out")
                    if config.camera_error:
                        raise RuntimeError(config.camera_error)
                    for index in range(config.camera_count):
                        if self.camera_stop.wait(0.02):
                            return
                        value = CameraObservation(self.clock)
                        target = Path(directory) / f"camera-window-{index // 2:03d}-{index % 2:03d}"
                        target.mkdir()
                        binding = persist(target, value)
                        row = {
                            "window_index": index // 2,
                            "frame_index": index % 2,
                            "camera": binding,
                            "host_return_at_s": value.host_return_stamp.value_ns / 1e9,
                            "source_sensor_stamp_ns": 1 if config.duplicate_stamp else index + 1,
                        }
                        with self.lock:
                            self.camera_updates_queue.append(row)
                except BaseException as error:
                    self.failure = error
                finally:
                    self.camera_done.set()

            self.camera_thread = threading.Thread(target=acquire, daemon=True)
            self.camera_thread.start()

        def camera_updates(self):
            with self.lock:
                result, self.camera_updates_queue = self.camera_updates_queue, []
                return result

        def camera_finished(self):
            if self.failure is not None:
                raise self.failure
            return self.camera_done.is_set()

        def stop_camera(self):
            self.camera_stop.set()
            if self.camera_thread is not None:
                self.camera_thread.join(3)
                assert not self.camera_thread.is_alive()
            events.append("camera-stopped")

        def _remaining(self):
            return self.deadline_s - self.clock.monotonic()

        def _inspect(self, identity):
            return {"Id": identity}

        def _owned(self, row):
            return "e" * 64 if config.owner_changed else row["Id"]

        def to_mapping(self):
            return {
                "CPU_fixture": True,
                "cleanup_verified": self.closed and not config.cleanup_error,
                "flight_authority": False,
            }

    class Operator:
        def __init__(self, selected, intent, directory, **kwargs):
            assert selected.receiver is None and intent.experiment_id == window.EXPERIMENT_ID
            self.intent = intent

        def apply(self):
            if config.primary_error:
                raise MemoryError(config.primary_error)
            events.append("CPU-operator")
            return {"CPU_fixture": True, "flight_authority": False}

        def to_mapping(self):
            return {"CPU_fixture": True, "flight_authority": False}

    def native(command, **kwargs):
        native_threads.append(threading.get_ident())
        assert threading.get_ident() != sessions[-1].main
        assert sessions[-1].receiver.to_mapping()["thread_alive"]
        assert command[:3] == ["CPU-not-Docker", "exec", "d" * 64]
        calls.append(command)
        native_entered.set()
        if not native_block.wait(2):
            raise RuntimeError("CPU native hold timed out")
        if config.native_error:
            raise MemoryError(config.native_error)
        if command[-1] in window.PARAMETERS:
            value = b"1\n" if command[-1] == "SIM_GZ_EN_GPS" else b"0\n"
            if config.drift_parameter and len(calls) > 16 and command[-1] == "EKF2_IMU_POS_X":
                value = b".1\n"
        else:
            value = b"CPU-original-topic\x00\xff"
            if "vehicle_local_position" in command:
                if "camera-start" not in events:
                    events.append("before-listener")
                else:
                    assert sessions[-1].camera_done.is_set()
                    events.append("after-listener")
        if config.source_drift and len(calls) == 37:
            runtime.write_bytes(b"{}")
        result = native_result(value)
        result["prefix_limit_bytes_per_stream"] = 256 * 1024
        result["output_limit_bytes"] = kwargs["output_limit_bytes"]
        if config.malformed_native:
            result.update(config.malformed_native)
        return result

    intent = window.DisposableSimOperatorIntent(
        "original-CPU",
        "CPU-container",
        tuple(SourceBinding("unused", 0, hashlib.sha256(b"").hexdigest()) for _ in range(5)),
        True,
    )
    return SimpleNamespace(
        root=tmp_path,
        output=tmp_path / "output",
        plan=plan,
        intent=intent,
        events=events,
        sessions=sessions,
        calls=calls,
        config=config,
        session=Session,
        operator=Operator,
        native=native,
        runtime=runtime,
        native_block=native_block,
        camera_block=camera_block,
        native_entered=native_entered,
        camera_entered=camera_entered,
        receive_threads=receive_threads,
        native_threads=native_threads,
    )


def run(h, **kwargs):
    return window.collect_origin_window(
        h.plan,
        h.output,
        execute=True,
        operator_intent=h.intent,
        artifact_root=h.root,
        session_factory=h.session,
        operator_factory=h.operator,
        native_runner=h.native,
        observation_s=kwargs.pop("observation_s", 5),
        **kwargs,
    )


def read_json(binding):
    raw = Path(binding["path"]).read_bytes()
    assert len(raw) == binding["bytes"] and hashlib.sha256(raw).hexdigest() == binding["sha256"]
    return json.loads(raw)


def test_full_real_V3_journal_and_two_bracketed_camera_windows(h):
    report = run(h)
    assert report["status"] == "origin-window-recorded", report["error"]
    assert len(h.calls) == 37 and len(report["camera_frames"]) == 4
    assert len(report["listener_snapshots"]) == 2 and report["host_camera_bracket_observed"]
    assert report["parameters_unchanged"] and len(report["preflight_snapshots"]) == 4
    assert report["loaded_world_sdf"]["native_output_semantics_parsed"] is False
    assert (
        report["raw_telemetry"]["record_count"] > 0
        and report["raw_telemetry"]["orphan_packet_count"] == 0
    )
    assert report["session"]["cleanup_verified"] and not report["receiver"]["thread_alive"]
    assert not report["native_owner"]["thread_alive"]
    assert not report["external_SIM_observed_here"] and report["injected_dependencies_used"]
    assert not any(report["claims"].values()) and not report["origin_qualified"]
    assert len(set(h.receive_threads)) == len(set(h.native_threads)) == 1
    assert set(h.receive_threads).isdisjoint(h.native_threads)
    assert h.intent.experiment_id == "original-CPU"
    assert set(report["snapshot_rows"]) == set(window.REQUIRED_MESSAGES)
    snapshot = read_json(report["snapshot_manifest"])
    assert snapshot["received_times_renewed"] is False
    assert snapshot["materialized_at_s"] < report["listener_snapshots"][1]["started_at_s"]
    receipt = read_json(report["receipt"])
    assert receipt["status"] == "origin-window-recorded"
    assert read_json(report["raw_telemetry"]["receipt"])["status"] == "recorded"
    for kind, rows in report["selected_telemetry"].items():
        assert rows and all(row["message"]["message_type"] == kind for row in rows)


@pytest.mark.parametrize("resource", ["native", "camera"])
def test_slow_native_or_camera_does_not_block_sole_receive_and_main_writer(h, resource):
    blocked = getattr(h, f"{resource}_block")
    entered = getattr(h, f"{resource}_entered")
    blocked.clear()
    observations = []

    def release():
        assert entered.wait(2)
        receiver = h.sessions[-1].receiver
        first = receiver.to_mapping()
        time.sleep(0.15)
        second = receiver.to_mapping()
        observations.append((first, second))
        blocked.set()

    thread = threading.Thread(target=release)
    thread.start()
    report = run(h)
    thread.join(3)
    assert report["status"] == "origin-window-recorded", report["error"]
    before, after = observations[0]
    assert after["received_packets"] > before["received_packets"]
    assert after["consumed_packets"] > before["consumed_packets"]
    assert not after["queue_overflow"]


@pytest.mark.parametrize(
    "name,message",
    [
        ("native_error", "native-primary"),
        ("camera_error", "camera-primary"),
        ("primary_error", "operator-primary"),
    ],
)
def test_primary_failure_still_joins_workers_and_cleans_container(h, name, message):
    setattr(h.config, name, message)
    report = run(h)
    assert report["status"] == "diagnostics-failed" and message in report["error"]
    assert report["session"]["cleanup_verified"]
    if "native_owner" in report:
        assert not report["native_owner"]["thread_alive"]
    if report["raw_telemetry"]:
        assert read_json(report["raw_telemetry"]["receipt"])["status"] == "aborted"


@pytest.mark.parametrize(
    "update",
    [
        {"native_exit_code": 1},
        {"native_exit_code": False},
        {"complete_output_observed": False},
        {"reader_threads_exited": False},
        {"timeout": True},
        {"owned_process_exited": False},
    ],
)
def test_native_failure_bytes_preserved_and_no_health_default(h, update):
    h.config.malformed_native = update
    report = run(h)
    assert report["status"] == "diagnostics-failed"
    assert report["session"]["cleanup_verified"] and len(h.calls) == 1
    row = report["native_owner"]["rows"][0]
    assert Path(row["streams"]["stdout"]["original_bytes"]["path"]).exists()
    assert not row["parameter_or_health_qualified"]


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("camera_count", 3, "four original"),
        ("duplicate_stamp", True, "repeat"),
        ("drift_parameter", True, "parameters changed"),
        ("owner_changed", True, "owned container"),
        ("missing_kind", "BATTERY_STATUS", "snapshot incomplete"),
    ],
)
def test_real_admission_gaps_refuse_with_partial_evidence(h, field, value, error):
    setattr(h.config, field, value)
    report = run(h)
    assert report["status"] == "diagnostics-failed" and error in report["error"]
    assert not report["origin_qualified"] and report["session"]["cleanup_verified"]


def test_late_runtime_source_drift_is_not_renewed(h):
    h.config.source_drift = True
    report = run(h)
    assert report["status"] == "diagnostics-failed"
    assert "drifted" in report["error"]
    assert report["session"]["cleanup_verified"] and not report["external_SIM_observed_here"]


def test_owned_cleanup_error_never_succeeds_even_with_complete_camera(h):
    h.config.cleanup_error = "actual owned cleanup failed"
    report = run(h)
    assert report["status"] == "diagnostics-failed"
    assert "cleanup failed" in report["error"]
    assert not report["session"]["cleanup_verified"]


def test_no_source_accepted_heartbeat_never_starts_native_or_camera(h):
    h.config.foreign = True
    report = run(h, observation_s=1)
    assert report["status"] == "diagnostics-failed" and "heartbeat" in report["error"]
    assert not h.calls and "camera-start" not in h.events
    assert report["raw_telemetry"]["record_count"] > 0


def test_default_plan_only_never_enters_native_receiver_camera_or_output(h):
    report = window.collect_origin_window(
        h.plan, h.output, operator_intent=h.intent, artifact_root=h.root
    )
    assert report["status"] == "planned" and not h.output.exists()
    assert not h.sessions and not h.calls and not report["origin_qualified"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"execute": 1},
        {"queue_capacity": True},
        {"observation_s": float("nan")},
        {"observation_s": 61},
    ],
)
def test_bad_scope_budget_types_fail_before_session(h, kwargs):
    with pytest.raises(ValueError):
        window.collect_origin_window(
            h.plan, h.output, operator_intent=h.intent, artifact_root=h.root, **kwargs
        )
    assert not h.sessions and not h.output.exists()


def test_create_only_no_old_artifact_overwrite(h):
    h.output.mkdir()
    marker = h.output / "old"
    marker.write_bytes(b"preserved")
    with pytest.raises(ValueError, match="Fresh contained"):
        run(h)
    assert marker.read_bytes() == b"preserved" and not h.sessions


def test_window_instance_cap_is_explicit_not_global_patch(tmp_path):
    from scripts import onboard_teacher_telemetry_capture_v3 as frozen

    capture = window.WindowTelemetryCapture(
        tmp_path / "journal",
        clock=lambda: 10,
        clock_id="CPU",
        transaction_id="CPU",
        local_origin_id="unresolved-CPU",
    )
    assert capture.max_records == 16384 and frozen.MAX_RECORDS == 8192
    receipt = capture.abort("CPU software test")
    assert receipt["status"] == "aborted" and receipt["contract"] == frozen.CONTRACT


def test_v3_requested_budget_still_cannot_silently_override_new_instance(tmp_path):
    with pytest.raises(ValueError, match="fixed record budget"):
        window.WindowTelemetryCapture(
            tmp_path / "journal",
            clock=lambda: 10,
            clock_id="CPU",
            transaction_id="CPU",
            local_origin_id="unresolved-CPU",
            max_records=32768,
        )
    assert not (tmp_path / "journal").exists()


def test_wrong_thread_native_submission_rejected(h):
    selected = h.session(h.plan, execute=True)
    owner = window.NativeReadOwner(selected, h.output, selected.deadline_s, runner=h.native).start()
    result = []

    def other():
        try:
            owner.submit("before")
        except BaseException as error:
            result.append(error)

    thread = threading.Thread(target=other)
    thread.start()
    thread.join()
    owner.close()
    assert isinstance(result[0], ValueError) and not h.calls


def test_phase_allowlist_never_contains_mutating_or_unbounded_command():
    rows = window.NativeReadOwner.specs("before") + window.NativeReadOwner.specs("after")
    assert len(rows) == 37
    assert sum(args == window.LISTENER_ARGS for _, args in rows) == 2
    assert not any("set" in args or "arm" in args or "mission" in args for _, args in rows)
    assert (
        window.WORLD_ARGS[-1] == "global_entity_gen_config { expand_include_tags { data: true } }"
    )
    with pytest.raises(ValueError):
        window.NativeReadOwner.specs("arbitrary-shell")


def test_fresh_import_loads_no_optional_ML_camera_or_mavlink():
    root = window.ROOT.as_posix()
    code = (
        f"import sys;sys.path[:0]=[{root!r},{str(window.ROOT / 'src')!r}];"
        "from scripts import collect_onboard_teacher_gazebo_origin_window;"
        "assert not any(k.split('.')[0] in {'torch','PIL','numpy','pymavlink','cv2'} "
        "for k in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")

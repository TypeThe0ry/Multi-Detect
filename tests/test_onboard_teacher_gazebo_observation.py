from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_onboard_teacher_gazebo_observation as runner


class Clock:
    session_id = "cpu-fixture-clock"

    def __init__(self):
        self.value = 10.0

    def monotonic(self):
        self.value += 0.002
        return self.value

    def utc_now(self):
        return datetime(2026, 10, 8, tzinfo=timezone.utc)

    def identity(self):
        return {"session_id": self.session_id, "source": "CPU-fixture-not-native-QPC"}


class Message:
    def __init__(self, name, source=(1, 1), fields=None):
        self.name, self.source = name, source
        self.fields = fields or {}

    def get_type(self):
        return self.name

    def get_srcSystem(self):
        return self.source[0]

    def get_srcComponent(self):
        return self.source[1]

    def get_seq(self):
        return 3

    def get_msgbuf(self):
        return b"CPU-constructed-packet-" + self.name.encode()

    def to_dict(self):
        return {"mavpackettype": self.name, **self.fields}


class Observation:
    rgb_bgr24 = b"\x01\x02\x03"

    def __init__(self, clock):
        self.depth = SimpleNamespace(payload=b"\x00\x00\x80\x3f")
        self.host_return_stamp = SimpleNamespace(value_ns=int(clock.monotonic() * 1e9))

    def to_mapping(self):
        return {
            "rgb_bgr24_sha256": hashlib.sha256(self.rgb_bgr24).hexdigest(),
            "depth": {"payload_sha256": hashlib.sha256(self.depth.payload).hexdigest()},
            "cpu_fixture": True,
        }


@pytest.fixture
def plan(tmp_path):
    source = tmp_path / "test.scenario.json"
    source.write_text("{}", encoding="utf-8")
    source.with_name("test.runtime.json").write_text(
        json.dumps({"world": {"wind_north_mps": 1.25, "wind_east_mps": -0.5}}),
        encoding="utf-8",
    )
    dock = SimpleNamespace(
        airframe_profile_id="px4_multirotor_gazebo_v1",
        vehicle_model="px4_multirotor_standard",
        model_name="x500_mono_cam",
        stack_plan_sha256="a" * 64,
        assets=SimpleNamespace(map=SimpleNamespace(path=source), dem=SimpleNamespace(path=source)),
    )
    return SimpleNamespace(
        source_manifest_path=source,
        docker_plan=dock,
        frame_count=2,
        to_mapping=lambda: {"cpu_fixture": True, "flight_authority": False},
    )


@pytest.fixture
def session_factory(monkeypatch):
    mavlink = SimpleNamespace(
        **{
            f"MAVLINK_MSG_ID_{name}": index
            for index, name in enumerate((*runner.REQUIRED_MESSAGES, *runner.EXTRA_MESSAGES))
        }
    )
    monkeypatch.setitem(
        sys.modules, "pymavlink", SimpleNamespace(mavutil=SimpleNamespace(mavlink=mavlink))
    )
    sessions = []

    class Session:
        def __init__(self, plan, *, execute):
            assert execute is True
            self.clock = Clock()
            self.sent = []
            self.peer_learned = False

            def send(*args):
                assert self.peer_learned, "No UDP reply peer before persisted receive"
                self.sent.append(args)

            self.connection = SimpleNamespace(mav=SimpleNamespace(command_long_send=send))
            self.messages = iter(
                [
                    Message("HEARTBEAT", (2, 1)),
                    *[Message(name) for name in runner.REQUIRED_MESSAGES],
                    *[Message(name) for name in runner.EXTRA_MESSAGES],
                ]
            )
            self.camera = SimpleNamespace(read=lambda: Observation(self.clock))
            self.closed = False
            sessions.append(self)

        def raw_receive(self, timeout):
            self.clock.value += timeout
            message = next(self.messages, None)
            if message is not None and message.source == (1, 1) and message.name == "HEARTBEAT":
                self.peer_learned = True
            return message

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def to_mapping(self):
            return {"closed": self.closed, "CPU_fixture": True}

        def attest_qualified(self, *, telemetry_qualified):
            assert telemetry_qualified is True
            return SimpleNamespace(to_mapping=lambda: {"CPU_fixture": True})

    Session.sessions = sessions
    return Session


def qualified(*args, **kwargs):
    assert set(args[0]) == set(runner.REQUIRED_MESSAGES)
    assert all(row["source_accepted"] is True for row in args[0].values())
    return SimpleNamespace(
        to_mapping=lambda: {"CPU_fixture_context": True},
        context=SimpleNamespace(captured_at_s=kwargs["now_s"] - 0.5),
    )


def test_plan_only_never_creates_session(plan, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network, Docker, camera or context during plan-only")

    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        session_factory=forbidden,
        context_builder=forbidden,
        artifact_root=tmp_path,
    )
    assert report["status"] == "planned"
    assert report["camera_frames_recorded"] == report["telemetry_stream_request_API_calls"] == 0
    assert all(value is False for value in report["claims"].values())


def test_fixture_observation_is_not_external_SIM(plan, session_factory, tmp_path):
    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        session_factory=session_factory,
        context_builder=qualified,
        artifact_root=tmp_path,
    )
    assert report["status"] == "observed-context-qualified"
    assert report["context_qualified"] is True
    assert report["external_SIM_observed_here"] is False
    assert report["camera_frames_recorded"] == 2
    assert report["raw_telemetry"]["foreign_source_count"] == 1
    assert report["raw_telemetry"]["accepted_source_count"] == 9
    assert report["session"]["closed"] is True
    assert len(session_factory.sessions[0].sent) == 11
    assert all(args[2] == 511 and args[3] == 0 for args in session_factory.sessions[0].sent)
    assert all(value is False for value in report["claims"].values())
    assert report["local_origin_id"].startswith("unresolved-ekf-origin-")
    camera = json.loads(Path(report["camera"][0]["path"]).read_bytes())
    for binding in camera["persisted_payloads"].values():
        raw = Path(binding["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == binding["sha256"]
    assert Path(report["receipt"]["path"]).is_file()


def test_missing_endurance_is_retained_refusal_not_constant(plan, session_factory, tmp_path):
    def refuse(*args, **kwargs):
        raise ValueError("time_remaining missing: no endurance override")

    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        context_timeout_s=4,
        session_factory=session_factory,
        context_builder=refuse,
        artifact_root=tmp_path,
    )
    assert report["status"] == "context-refused"
    assert "endurance override" in report["context_error"]
    assert report["context_qualified"] is False
    assert report["raw_telemetry"]["status"] == "recorded"
    assert report["raw_telemetry"]["accepted_source_count"] == 11
    assert report["session"]["closed"] is True
    assert not (tmp_path / "output/observed-context.json").exists()


def test_camera_failure_seals_aborted_raw_and_cleans_session(plan, session_factory, tmp_path):
    class BadCameraSession(session_factory):
        def __enter__(self):
            def fail():
                raise RuntimeError("Native camera failed")

            self.camera.read = fail
            return self

    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        session_factory=BadCameraSession,
        context_builder=qualified,
        artifact_root=tmp_path,
    )
    assert report["status"] == "observation-failed"
    assert "Native camera failed" in report["error"]
    assert report["raw_telemetry"]["status"] == "aborted"
    assert report["session"]["closed"] is True


def test_enter_failure_still_retains_session_receipt(plan, session_factory, tmp_path):
    class BadEntry(session_factory):
        def __enter__(self):
            self.closed = True
            raise RuntimeError("Pinned image unavailable")

    report = runner.observe_teacher(
        plan, tmp_path / "output", execute=True, session_factory=BadEntry, artifact_root=tmp_path
    )
    assert report["status"] == "observation-failed"
    assert report["session"]["closed"] is True
    assert report["raw_telemetry"] is None
    assert report["camera_frames_recorded"] == 0


def test_cleanup_failure_is_not_native_success(plan, session_factory, tmp_path):
    class BadExit(session_factory):
        def __exit__(self, *args):
            self.closed = True
            raise RuntimeError("Owned cleanup not verified")

    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        session_factory=BadExit,
        context_builder=qualified,
        artifact_root=tmp_path,
    )
    assert report["status"] == "observation-failed"
    assert "cleanup not verified" in report["error"]
    assert report["context_qualified"] is True
    assert report["claims"]["flight_executed"] is False


def test_foreign_nonfinite_packet_is_not_silently_discarded(plan, session_factory, tmp_path):
    class ForeignNan(session_factory):
        def __enter__(self):
            self.messages = iter([Message("VFR_HUD", (2, 1), {"airspeed": float("nan")})])
            return self

    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        session_factory=ForeignNan,
        context_builder=qualified,
        artifact_root=tmp_path,
    )
    assert report["status"] == "observation-failed"
    assert report["raw_telemetry"]["status"] == "aborted"
    assert report["raw_telemetry"]["record_count"] == 0
    assert report["context_qualified"] is False


def test_resources_keep_unavailable_flags_and_explicit_configured_wind(
    plan, session_factory, tmp_path
):
    session = session_factory(plan, execute=True)
    runtime, source = runner._runtime_source(plan, tmp_path)
    assert runner._verify_source(source, tmp_path)
    resources = runner._resources(
        plan,
        runner._profile(plan),
        session,
        9.0,
        now_s=10.0,
        transaction_id="observe-only",
        runtime=runtime,
    )
    assert resources["wind_north_mps"] == 1.25
    assert resources["wind_source"] == "software-SIM-configured"
    assert resources["geofence_valid"] is resources["perception_available"] is False
    assert resources["camera_available"] is True
    assert (
        runner._resources(
            plan,
            runner._profile(plan),
            session,
            7,
            now_s=10,
            transaction_id="observe-only",
            runtime=runtime,
        )["camera_available"]
        is False
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("execute", 1),
        ("context_timeout_s", 0),
        ("context_timeout_s", 61),
        ("maximum_records", 15),
        ("maximum_records", 8193),
    ],
)
def test_input_bounds_before_any_output(plan, tmp_path, key, value):
    with pytest.raises((TypeError, ValueError)):
        runner.observe_teacher(plan, tmp_path / "output", artifact_root=tmp_path, **{key: value})
    assert not (tmp_path / "output").exists()


def test_cannot_overwrite_existing_observation(plan, tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "user-file.txt"
    sentinel.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        runner.observe_teacher(plan, output, artifact_root=tmp_path)
    assert sentinel.read_bytes() == b"preserve"


def test_bad_camera_binding_refuses_label_metadata(tmp_path):
    observation = Observation(Clock())
    observation.rgb_bgr24 = b"changed"

    def wrong():
        return {"rgb_bgr24_sha256": "0" * 64, "depth": {"payload_sha256": "0" * 64}}

    observation.to_mapping = wrong
    with pytest.raises(ValueError, match="does not bind"):
        runner._persist_camera(tmp_path, observation)
    assert not (tmp_path / "camera.json").exists()


def test_no_initial_heartbeat_no_requests_or_camera(plan, session_factory, tmp_path):
    class NoHeartbeat(session_factory):
        def __enter__(self):
            self.messages = iter([Message("GPS_RAW_INT")])
            return self

    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        context_timeout_s=1,
        session_factory=NoHeartbeat,
        context_builder=qualified,
        artifact_root=tmp_path,
    )
    assert report["status"] == "observation-failed"
    assert "HEARTBEAT" in report["error"]
    assert report["telemetry_stream_request_API_calls"] == report["camera_frames_recorded"] == 0
    assert session_factory.sessions[-1].sent == []


def test_original_runtime_drift_refuses_context(plan, session_factory, tmp_path):
    def drift(*args, **kwargs):
        Path(tmp_path / "test.runtime.json").write_text("{}", encoding="utf-8")
        return qualified(*args, **kwargs)

    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        context_timeout_s=3,
        session_factory=session_factory,
        context_builder=drift,
        artifact_root=tmp_path,
    )
    assert report["context_qualified"] is False
    assert report["status"] == "observation-failed"
    assert "drifted" in report["error"]


def test_slow_source_qualification_is_not_fresh_context(plan, session_factory, tmp_path):
    def slow(*args, **kwargs):
        context = qualified(*args, **kwargs)
        session_factory.sessions[-1].clock.value += 2.1
        return context

    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        context_timeout_s=3,
        session_factory=session_factory,
        context_builder=slow,
        artifact_root=tmp_path,
    )
    assert report["context_qualified"] is False
    assert report["status"] == "context-refused"
    assert "stale during source reads" in report["context_error"]


@pytest.mark.parametrize("operation", ["seal", "abort"])
def test_capture_close_failure_preserves_primary_error(
    plan, session_factory, tmp_path, monkeypatch, operation
):
    Original = runner.TeacherTelemetryCapture

    class BrokenClose(Original):
        def seal(self):
            if operation == "seal":
                self._closed = True
                raise OSError("original receipt fsync failure")
            return super().seal()

        def abort(self, reason):
            if operation == "abort":
                raise OSError("secondary abort write failure")
            return super().abort(reason)

    monkeypatch.setattr(runner, "TeacherTelemetryCapture", BrokenClose)

    class BadSession(session_factory):
        def raw_receive(self, timeout):
            if operation == "abort":
                raise OSError("original receive failure")
            return super().raw_receive(timeout)

    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        session_factory=BadSession,
        context_builder=qualified,
        artifact_root=tmp_path,
    )
    assert report["status"] == "observation-failed"
    assert "capture_close_error" in report and report["capture_close_error"]
    assert (
        "original receipt fsync failure" in report["error"]
        if operation == "seal"
        else "original receive failure" in report["error"]
    )
    assert report["session"]["closed"] is True
    assert Path(report["receipt"]["path"]).is_file()


def test_session_report_failure_does_not_erase_observation(plan, session_factory, tmp_path):
    class BadReport(session_factory):
        def to_mapping(self):
            raise RuntimeError("detached session report failed")

    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        session_factory=BadReport,
        context_builder=qualified,
        artifact_root=tmp_path,
    )
    assert report["status"] == "observation-failed"
    assert "report failed" in report["session_report_error"]
    assert Path(report["receipt"]["path"]).is_file()


def test_slow_context_persistence_is_not_fresh_context(
    plan, session_factory, tmp_path, monkeypatch
):
    original = runner._json_new

    def slow_write(path, value):
        result = original(path, value)
        if Path(path).name.startswith("observed-context-"):
            session_factory.sessions[-1].clock.value += 2.1
        return result

    monkeypatch.setattr(runner, "_json_new", slow_write)
    report = runner.observe_teacher(
        plan,
        tmp_path / "output",
        execute=True,
        context_timeout_s=3,
        session_factory=session_factory,
        context_builder=qualified,
        artifact_root=tmp_path,
    )
    assert report["context_qualified"] is False
    assert report["status"] == "context-refused"
    assert "stale during persistence" in report["context_error"]
    assert not report.get("context")

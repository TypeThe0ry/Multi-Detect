"""CPU orchestration stand-ins, not Docker/PX4/camera/hardware evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.clock import HighResolutionSystemClock
from scripts import onboard_teacher_gazebo_session as session_api

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = "a" * 64


@pytest.fixture(scope="module")
def docker_template(tmp_path_factory):
    # Real repository plan/assets, no native process, network or image decoding.
    plan = session_api.build_teacher_session_plan(
        ROOT / "configs/scenarios/urban_inspection_multirotor_001.scenario.json",
        tmp_path_factory.mktemp("teacher-planning"),
        frame_count=2,
    )
    assert plan.docker_plan.host_control_port == 14581
    assert plan.docker_plan.host_telemetry_port == 14582
    return replace(plan.docker_plan, docker_executable="docker-standin")


@pytest.fixture
def plan(tmp_path, docker_template):
    return session_api.TeacherSessionPlan(
        docker_template,
        ROOT / "configs/scenarios/urban_inspection_multirotor_001.scenario.json",
        tmp_path / "owned",
        frame_count=2,
        camera_timeout_s=0.5,
        session_timeout_s=5,
        width=2,
        height=1,
    )


class RawConnection:
    def __init__(self):
        self.messages = [object(), object()]
        self.calls = []
        self.closed = False
        self.error = None

    def recv_match(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.messages.pop(0) if self.messages else None

    def close(self):
        self.closed = True


class NativeDocker:
    def __init__(self, plan):
        self.plan = plan.docker_plan
        self.records = []
        self.current = None
        self.error = None
        self.launch_failure = False
        self.stdout_id = IDENTITY
        self.cid_id = IDENTITY
        self.stop_code = self.rm_code = 0
        self.inspect_unknown = False

    def __call__(self, argv, *, timeout_s):
        assert 0 < timeout_s <= 30
        assert argv[0] == "docker-standin"
        arguments = argv[1:]
        self.records.append(arguments)
        if self.error is not None:
            raise self.error
        if arguments[:2] == ["container", "inspect"]:
            if self.inspect_unknown:
                return subprocess.CompletedProcess(argv, 1, "", "daemon unavailable")
            if self.current is None:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    "",
                    "Error: No such object: " + arguments[2],
                )
            return subprocess.CompletedProcess(argv, 0, json.dumps([self.current]), "")
        if arguments[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if arguments[0] == "run":
            labels = {}
            for index, value in enumerate(arguments):
                if value == "--label":
                    name, text = arguments[index + 1].split("=", 1)
                    labels[name] = text
            mounts = [
                {
                    "Target": "/opt/multidetect-gazebo-models",
                    "Source": str(self.plan.model_root),
                    "ReadOnly": True,
                },
                {
                    "Target": "/opt/px4-gazebo/share/gz/worlds/default.sdf",
                    "Source": str(self.plan.world_path),
                    "ReadOnly": True,
                },
                {
                    "Target": "/workspace/scripts",
                    "Source": str(self.plan.scripts_root),
                    "ReadOnly": True,
                },
            ]
            if self.plan.fuel_resolved_root is not None:
                mounts.append(
                    {
                        "Target": "/opt/multidetect-gz-fuel-resolved",
                        "Source": str(self.plan.fuel_resolved_root),
                        "ReadOnly": True,
                    }
                )
            self.current = {
                "Id": IDENTITY,
                "Name": "/" + self.plan.container_name,
                "Config": {
                    "Image": self.plan.image,
                    "Labels": labels,
                    "Env": [f"{k}={v}" for k, v in self.plan.environment],
                },
                "HostConfig": {
                    "NetworkMode": "bridge",
                    "Privileged": False,
                    "Devices": [],
                    "Mounts": mounts,
                    "PortBindings": {
                        "14581/udp": [{"HostIp": "127.0.0.1", "HostPort": "14581"}],
                    },
                },
                "State": {"Running": True},
                "Mounts": [
                    {"Source": m["Source"], "Destination": m["Target"], "RW": False} for m in mounts
                ],
            }
            cid = Path(arguments[arguments.index("--cidfile") + 1])
            with cid.open("x") as stream:
                stream.write(self.cid_id + "\n")
            return subprocess.CompletedProcess(
                argv,
                1 if self.launch_failure else 0,
                self.stdout_id + "\n",
                "",
            )
        if arguments[0] == "stop":
            assert arguments == ["stop", "--timeout", "2", IDENTITY]
            self.current["State"]["Running"] = False
            return subprocess.CompletedProcess(argv, self.stop_code, "", "")
        if arguments[0] == "rm":
            assert arguments == ["rm", IDENTITY]
            if self.rm_code == 0:
                self.current = None
            return subprocess.CompletedProcess(argv, self.rm_code, "", "")
        raise AssertionError(f"Unexpected native command {arguments}")


class Pixels:
    def __init__(self, raw):
        self.raw = raw

    def tobytes(self, *, order):
        assert order == "C"
        return self.raw


class Provenance(SimpleNamespace):
    def to_mapping(self):
        return dict(vars(self))


class CameraSource:
    def __init__(self, plan, clock):
        self.plan, self.clock = plan, clock
        self.reads = 0
        self.opened = self.closed = False
        self.close_error = self.read_error = None
        self.helper_exit_code = None
        self.block = None
        self.mutate = None
        self.latest_depth_frame = None

    def open(self):
        self.opened = True

    def read(self):
        if self.block is not None:
            self.block.wait(2)
        if self.read_error is not None:
            raise self.read_error
        raw = bytes([self.reads + 1]) * 6
        header = b"actual-standin-header"
        captured = self.clock.monotonic()
        frame = SimpleNamespace(
            frame_id=f"real-standin-{self.reads}",
            captured_at_s=captured,
            width=2,
            height=1,
            image_bgr=Pixels(raw),
        )
        self.latest_frame_provenance = Provenance(
            frame_id=frame.frame_id,
            captured_at_s=captured,
            host_clock_identity=tuple(self.clock.identity().items()),
            sequence=self.reads,
            pixel_format="BGR24",
            width=2,
            height=1,
            stride=6,
            payload_bytes=6,
            payload_sha256=hashlib.sha256(raw).hexdigest(),
            record_sha256=hashlib.sha256(header + raw).hexdigest(),
            wire_unsigned_header_hex=header.hex(),
            sensor_stamp_ns=1000 + self.reads,
            camera_info_stamp_ns=900 + self.reads,
            navsat_stamp_ns=850 + self.reads,
            odometry_stamp_ns=950 + self.reads,
            wire_telemetry={
                "orientation_x": 0.1,
                "orientation_y": 0.2,
                "orientation_z": 0.3,
                "orientation_w": 0.92736185,
            },
            depth_source_stamp_ns=None,
            depth_payload_sha256=None,
        )
        if self.mutate is not None:
            self.mutate(frame, self.latest_frame_provenance)
        self.reads += 1
        return frame

    def close(self):
        self.closed = True
        self.helper_exit_code = 0 if self.reads == self.plan.frame_count else -15
        if self.block is not None:
            self.block.set()
        if self.close_error is not None:
            raise self.close_error

    @property
    def stats(self):
        row = {
            "helper_exit_code": self.helper_exit_code,
            "complete": self.closed
            and self.reads == self.plan.frame_count
            and self.helper_exit_code == 0,
        }
        return SimpleNamespace(**row, to_mapping=lambda: dict(row))


class Backend:
    def __init__(self, plan, clock):
        self.clock = clock
        self.frame_source = CameraSource(plan, clock)

    def close(self):
        self.frame_source.close()


@pytest.fixture
def harness(plan):
    docker = NativeDocker(plan)
    connection = RawConnection()
    clock = HighResolutionSystemClock()
    backend = Backend(plan, clock)
    probes, connections, camera_calls = [], [], []

    def connect(endpoint, **kwargs):
        connections.append((endpoint, kwargs))
        return connection

    def camera(given_plan, identity, given_clock):
        assert given_plan is plan and identity == IDENTITY and given_clock is clock
        camera_calls.append(identity)
        return backend

    session = session_api.OwnedTeacherGazeboSession(
        plan,
        execute=True,
        clock=clock,
        runner=docker,
        connection_factory=connect,
        camera_factory=camera,
        port_probe=lambda given: probes.append(given),
    )
    return SimpleNamespace(
        session=session,
        docker=docker,
        connection=connection,
        backend=backend,
        probes=probes,
        connections=connections,
        camera_calls=camera_calls,
    )


def assert_cleaned(harness):
    assert harness.connection.closed
    assert harness.docker.current is None
    assert [r for r in harness.docker.records if r[0] in ("stop", "rm")] == [
        ["stop", "--timeout", "2", IDENTITY],
        ["rm", IDENTITY],
    ]


def test_default_plan_only_has_no_process_socket_or_evidence(plan):
    def forbidden(*args, **kwargs):
        raise AssertionError("No external operation permitted")

    with session_api.OwnedTeacherGazeboSession(
        plan,
        runner=forbidden,
        connection_factory=forbidden,
        camera_factory=forbidden,
        port_probe=forbidden,
    ) as session:
        assert session.camera is None and session.connection is None
        with pytest.raises(session_api.TeacherSessionError):
            session.raw_receive(1)
    assert session.to_mapping()["status"] == "planned_only"
    assert not plan.evidence_dir.exists()


def test_actual_plan_is_pinned_readonly_isolated_and_multirotor(docker_template):
    from scripts.run_simulator_docker import PINNED_IMAGE

    assert docker_template.image == PINNED_IMAGE
    row = docker_template.to_mapping()
    assert row["validation"]["mounts_readonly"] is True
    assert row["ports"]["telemetry"]["published"] is False
    assert row["vehicle"]["airframe_profile_id"] == "px4_multirotor_gazebo_v1"


def test_real_session_uses_original_recv_and_same_clock_full_wire(harness):
    first_message = harness.connection.messages[0]
    with harness.session as session:
        assert not harness.connection.calls  # no heartbeat/probe/qualification receive
        assert session.raw_receive(0.1) is first_message
        assert harness.connection.calls == [{"blocking": True, "timeout": 0.1}]
        # The listen host depends on the SIM network mode (loopback for host
        # networking, the docker bridge gateway otherwise).
        from multidetect.sim_network import sim_udpin_endpoint

        assert harness.connections == [
            (
                sim_udpin_endpoint(14582),
                {"source_system": 250, "source_component": 191, "autoreconnect": False},
            )
        ]
        one, two = session.camera.read(), session.camera.read()
        assert one.rgb_bgr24 != two.rgb_bgr24
        assert one.frame.frame_id != two.frame.frame_id
        assert one.provenance.sensor_stamp_ns == 1000
        assert one.to_mapping()["provenance"]["wire_telemetry"]["orientation_x"] == 0.1
        assert one.to_mapping()["transcoded"] is False
        assert one.host_return_stamp.session_id == session.clock.session_id
        assert session.camera.clock is session.clock
        with pytest.raises(session_api.TeacherSessionError, match="budget"):
            session.camera.read()
    assert_cleaned(harness)
    row = session.to_mapping()
    assert row["cleanup_verified"] is True and row["status"] == "closed"
    assert row["camera"]["native_stats"]["helper_exit_code"] == 0
    assert row["camera"]["admitted_frame_count"] == 2
    row["native_commands"].clear()
    assert session.to_mapping()["native_commands"]
    receipt = json.loads((session.plan.evidence_dir / "session-receipt.json").read_text())
    assert receipt["status"] == "closed"


def test_no_qualification_is_fabricated(harness):
    with harness.session as session:
        for value in (False, None, 1):
            with pytest.raises(session_api.TeacherSessionError, match="qualification"):
                session.attest_qualified(telemetry_qualified=value)
        attestation = session.attest_qualified(telemetry_qualified=True)
        assert attestation.telemetry_qualified is True
        assert attestation.hardware_control_enabled is False
        assert not harness.connection.calls
    assert_cleaned(harness)


@pytest.mark.parametrize(
    "field,value",
    [
        ("frame_count", True),
        ("frame_count", 0),
        ("frame_count", 129),
        ("width", True),
        ("width", 0),
        ("height", 4097),
        ("camera_timeout_s", float("nan")),
        ("camera_timeout_s", 6),
        ("session_timeout_s", True),
        ("session_timeout_s", 601),
        ("width", 4096),  # with count/height replaced separately below for pixel cap
    ],
)
def test_plan_numeric_budgets_fail_closed(plan, field, value):
    if field == "width" and value == 4096:
        with pytest.raises(ValueError, match="capture budget"):
            replace(plan, width=value, height=4096, frame_count=128)
    else:
        with pytest.raises(ValueError):
            replace(plan, **{field: value})


@pytest.mark.parametrize(
    "change",
    [
        {"host_control_port": 14550},
        {"host_telemetry_port": 14581},
        {"airframe_profile_id": "px4_vtol_gazebo_v1"},
        {"image": "unbound:latest"},
    ],
)
def test_plan_container_identity_fields_fail(plan, change):
    with pytest.raises(ValueError):
        replace(plan, docker_plan=replace(plan.docker_plan, **change))


def test_existing_evidence_directory_and_repeat_entry_reject(harness):
    with harness.session:
        pass
    with pytest.raises(ValueError, match="Fresh"):
        replace(harness.session.plan)
    with pytest.raises(session_api.TeacherSessionError, match="single-use"):
        harness.session.__enter__()


def test_unknown_inspect_failure_never_launches_or_cleans(harness):
    harness.docker.inspect_unknown = True
    with pytest.raises(session_api.TeacherSessionError, match="absence not proven"):
        with harness.session:
            pass
    assert len(harness.docker.records) == 1
    assert not harness.connections and not harness.camera_calls


def test_existing_unrelated_container_not_touched(harness):
    harness.docker.current = {"Id": "b" * 64}
    with pytest.raises(session_api.TeacherSessionError, match="Pre-existing"):
        with harness.session:
            pass
    assert len(harness.docker.records) == 1
    assert not harness.connections


def test_launch_failure_recovers_only_own_label_and_native_id(harness):
    harness.docker.launch_failure = True
    with pytest.raises(session_api.TeacherSessionError, match="launch native"):
        with harness.session:
            pass
    assert_cleaned(harness)
    assert harness.session.to_mapping()["status"] == "failed"
    assert harness.session.to_mapping()["executed"] is False


def test_stdout_cid_mismatch_refuses_other_container_cleanup(harness):
    harness.docker.stdout_id = "b" * 64
    with pytest.raises(session_api.TeacherSessionError, match="identity mismatch"):
        with harness.session:
            pass
    assert_cleaned(harness)  # recovery from our exact token, not the incorrect stdout ID


def test_late_owner_mutation_does_not_stop_or_rm(harness):
    with pytest.raises(session_api.TeacherSessionError, match="close cleanly"):
        with harness.session:
            harness.docker.current["Config"]["Labels"][session_api.OWNER_LABEL] = "foreign"
    assert not any(r[0] in ("stop", "rm") for r in harness.docker.records)
    assert harness.connection.closed
    assert harness.session.to_mapping()["cleanup_verified"] is False


@pytest.mark.parametrize("operation", ["stop", "rm"])
def test_cleanup_native_failure_never_success(harness, operation):
    setattr(harness.docker, operation + "_code", 1)
    with pytest.raises(session_api.TeacherSessionError, match="close cleanly"):
        with harness.session:
            pass
    assert harness.session.to_mapping()["cleanup_verified"] is False
    assert harness.session.to_mapping()["status"] == "failed"
    assert any(r[0] == "rm" for r in harness.docker.records)


@pytest.mark.parametrize("error", [MemoryError("body"), KeyboardInterrupt("body")])
def test_body_baseexception_preserved_and_owned_cleanup_done(harness, error):
    with pytest.raises(type(error), match="body"):
        with harness.session:
            raise error
    assert_cleaned(harness)
    assert harness.session.to_mapping()["status"] == "failed"


@pytest.mark.parametrize("operation", ["stop", "rm"])
def test_cleanup_exception_still_attempts_rm_and_final_inspect(harness, operation):
    native = harness.docker

    def run(argv, *, timeout_s):
        if argv[1] == operation:
            native.records.append(argv[1:])
            raise subprocess.TimeoutExpired(argv, timeout_s)
        return native(argv, timeout_s=timeout_s)

    harness.session._runner = run
    with pytest.raises(session_api.TeacherSessionError, match="close cleanly"):
        with harness.session:
            pass
    assert any(row[0] == "rm" for row in native.records)
    assert native.records[-1] == ["container", "inspect", IDENTITY]
    assert harness.connection.closed
    assert harness.session.to_mapping()["cleanup_verified"] is False
    assert harness.session.to_mapping()["status"] == "failed"


def test_cleanup_exception_keeps_original_body_error(harness):
    native = harness.docker

    def run(argv, *, timeout_s):
        if argv[1] == "stop":
            native.records.append(argv[1:])
            raise OSError("stop transport")
        return native(argv, timeout_s=timeout_s)

    harness.session._runner = run
    with pytest.raises(MemoryError, match="body") as caught:
        with harness.session:
            raise MemoryError("body")
    assert_cleaned(harness)
    assert caught.value.__notes__
    assert harness.session.to_mapping()["cleanup_verified"] is False


def test_partial_camera_not_declared_complete_on_normal_exit(harness):
    with pytest.raises(session_api.TeacherSessionError, match="close cleanly"):
        with harness.session as session:
            session.camera.read()
    assert_cleaned(harness)
    assert harness.session.to_mapping()["camera"]["native_stats"]["complete"] is False


def test_camera_native_close_error_still_closes_connection_and_container(harness):
    with pytest.raises(MemoryError, match="original") as caught:
        with harness.session as session:
            session.camera.read()
            harness.backend.frame_source.close_error = OSError("camera close")
            raise MemoryError("original")
    assert_cleaned(harness)
    assert any("cleanup" in note for note in caught.value.__notes__)


def test_receive_exception_poison_prevents_reuse(harness):
    harness.connection.error = MemoryError("read")
    with pytest.raises(session_api.TeacherSessionError, match="close cleanly"):
        with harness.session as session:
            with pytest.raises(MemoryError):
                session.raw_receive(1)
            with pytest.raises(session_api.TeacherSessionError):
                session.raw_receive(1)
    assert_cleaned(harness)
    assert len(harness.connection.calls) == 1


def test_receiver_cannot_be_replaced_or_called_reentrantly(harness):
    with harness.session as session:
        original = harness.connection.recv_match
        harness.connection.recv_match = lambda **kwargs: None
        with pytest.raises(session_api.TeacherSessionError):
            session.raw_receive(1)
        harness.connection.recv_match = original
        session._receiving = True
        with pytest.raises(session_api.TeacherSessionError):
            session.raw_receive(1)
        session._receiving = False


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload_sha256", "f" * 64),
        ("record_sha256", "f" * 64),
        ("frame_id", "wrong"),
        ("pixel_format", "RGB24"),
        ("stride", 100),
        ("host_clock_identity", (("session_id", "other"),)),
    ],
)
def test_camera_metadata_or_pixels_drift_rejected(harness, field, value):
    harness.backend.frame_source.mutate = lambda frame, row: setattr(row, field, value)
    with pytest.raises(session_api.TeacherSessionError, match="binding failed"):
        with harness.session as session:
            session.camera.read()
    assert harness.session.camera.count == 0
    assert_cleaned(harness)


def test_camera_duplicate_sequence_rejected_no_frame_reuse(harness):
    with pytest.raises(session_api.TeacherSessionError, match="binding failed"):
        with harness.session as session:
            session.camera.read()
            harness.backend.frame_source.mutate = lambda frame, row: setattr(row, "sequence", 0)
            session.camera.read()
    assert harness.session.camera.count == 1
    assert_cleaned(harness)


def test_camera_endless_read_deadline_closes_only_its_helper(harness):
    harness.backend.frame_source.block = threading.Event()
    with pytest.raises(session_api.TeacherSessionError, match="deadline exceeded"):
        with harness.session as session:
            session.camera.read(deadline_s=session.clock.monotonic() + 0.02)
    assert harness.backend.frame_source.closed
    assert harness.session.camera.count == 0
    assert_cleaned(harness)


def test_caught_camera_timeout_still_cannot_close_successfully(harness):
    harness.backend.frame_source.block = threading.Event()
    with pytest.raises(session_api.TeacherSessionError, match="close cleanly"):
        with harness.session as session:
            with pytest.raises(session_api.TeacherSessionError, match="deadline exceeded"):
                session.camera.read(deadline_s=session.clock.monotonic() + 0.02)
    assert_cleaned(harness)
    assert harness.session.to_mapping()["status"] == "failed"


def test_raw_receive_and_camera_wrong_thread_rejected(harness):
    with harness.session as session:
        errors = []

        def foreign():
            for operation in (lambda: session.raw_receive(0), session.camera.read):
                try:
                    operation()
                except session_api.TeacherSessionError as error:
                    errors.append(error)

        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join(2)
        assert len(errors) == 2
        assert not harness.connection.calls and harness.backend.frame_source.reads == 0


def test_factory_clock_mismatch_closes_failed_backend_and_owned_container(harness):
    harness.backend.frame_source.clock = HighResolutionSystemClock()
    with pytest.raises(session_api.TeacherSessionError, match="exact session clock"):
        with harness.session:
            pass
    assert harness.backend.frame_source.closed
    assert_cleaned(harness)


def test_cleanup_claim_fsync_failure_does_not_skip_owned_native_cleanup(harness, monkeypatch):
    real = session_api._write_new

    def write(path, row):
        if Path(path).name == "cleanup-claim.json":
            raise OSError("claim fsync")
        return real(path, row)

    monkeypatch.setattr(session_api, "_write_new", write)
    with pytest.raises(session_api.TeacherSessionError, match="close cleanly"):
        with harness.session:
            pass
    assert_cleaned(harness)
    assert harness.session.to_mapping()["status"] == "failed"


def test_guarded_module_import_has_no_mavlink_numpy_ml_or_native_start():
    code = (
        "import importlib.util,sys; "
        "s=importlib.util.spec_from_file_location('teacher_session',"
        f"{str(session_api.__file__)!r}); "
        "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;s.loader.exec_module(m); "
        "assert not any(x in sys.modules for x in "
        "('pymavlink','numpy','PIL','torch','transformers')); print('stdlib-import-only')"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "stdlib-import-only"


def test_cli_only_plans_and_has_no_execute_flag(plan, monkeypatch, capsys):
    monkeypatch.setattr(session_api, "build_teacher_session_plan", lambda *a, **kw: plan)
    assert session_api.main(["--scenario", "standin", "--job-dir", "standin"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["default_plan_only"] is True and row["flight_authority"] is False
    with pytest.raises(SystemExit) as caught:
        session_api.main(["--scenario", "standin", "--job-dir", "standin", "--execute"])
    assert caught.value.code == 2
    assert not plan.evidence_dir.exists()

"""CPU source-owner seams + real open V3 wire/qualifier, NOT Gazebo evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy.teacher_live_navigation_checkpoint import (
    LiveTeacherNavigationCheckpoint,
)
from scripts import collect_teacher_live_navigation_checkpoint_v1 as provider

_SPEC = importlib.util.spec_from_file_location(
    "_live_checkpoint_provider_cpu_helpers",
    Path(__file__).with_name("test_teacher_live_navigation_checkpoint.py"),
)
helpers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(helpers)


@pytest.fixture
def collection_fixture(tmp_path, monkeypatch):
    generator = helpers.live_fixture.__wrapped__(tmp_path)
    args, capture, clock, _ = next(generator)
    # Explicit interface stand-ins: no session enter, Docker or camera launch.
    plan = object.__new__(provider.TeacherSessionPlan)
    values = {
        "frame_count": 2,
        "camera_timeout_s": 1,
        "session_timeout_s": 20,
        "width": 2,
        "height": 2,
        "source_manifest_path": tmp_path / "fixture.scenario.json",
        "evidence_dir": tmp_path / "CPU-session",
        "docker_plan": SimpleNamespace(
            docker_executable="docker",
            to_mapping_with_hash=lambda: args["plan"]["docker_plan"],
        ),
    }
    for name, value in values.items():
        object.__setattr__(plan, name, value)
    session = object.__new__(provider.OwnedTeacherGazeboSessionV3)
    session.plan, session.clock = plan, clock
    session._owner = threading.get_ident()
    session._entered, session.execute = True, True
    session._closed, session._failure = False, None
    session.deadline_s = 120
    session.container_id, session.owner_token = args["container_id"], args["owner_token"]
    session._active_camera, session._camera_thread = None, None
    session._camera_lock = threading.Lock()

    def empty_reader(_):
        threading.Event().wait(0.002)
        return None

    receiver = provider.ConcurrentTelemetryReceiver(empty_reader, clock.monotonic)
    receiver.start()
    session.receiver = receiver

    class FixtureOwner(provider._SourceOwner):
        def _run(self):
            # Source bytes come from independent CPU wire fixtures, NOT native.
            self.rows = {
                "parameter-baseline": copy.deepcopy(args["parameter_baseline"]),
                "navigation-before": copy.deepcopy(args["navigation_before"]),
                "navigation-after": copy.deepcopy(args["navigation_after"]),
                "owned-inspect-before": copy.deepcopy(args["owned_inspect_before"]),
                "owned-inspect-after": copy.deepcopy(args["owned_inspect_after"]),
            }
            self.baseline = self.baseline or self.rows["parameter-baseline"]
            self.camera_updates = copy.deepcopy(args["camera_updates"])
            self.done.set()

    monkeypatch.setattr(provider, "_SourceOwner", FixtureOwner)
    inputs = dict(
        profile_source=args["profile_source"],
        runtime_source=args["runtime_source"],
        world_source=args["world_source"],
        model_sources=args["model_sources"],
    )
    try:
        yield session, receiver, capture, plan, args, inputs
    finally:
        receiver.stop()
        generator.close()


def call(fixture, output_name="collected", **changes):
    session, receiver, capture, plan, args, inputs = fixture
    return provider.collect_live_checkpoint(
        session,
        receiver,
        capture,
        plan,
        args["artifact_root"],
        args["artifact_root"] / output_name,
        **{**inputs, **changes},
    )


def receipt(fixture, output_name="collected"):
    return json.loads((fixture[4]["artifact_root"] / output_name / "collection.json").read_bytes())


def test_real_open_wire_qualifier_via_cpu_source_owner_seam(collection_fixture):
    session, receiver, capture, _, args, _ = collection_fixture
    checkpoint = call(collection_fixture)
    assert isinstance(checkpoint, LiveTeacherNavigationCheckpoint)
    assert checkpoint._live_capture is capture and checkpoint._live_clock is session.clock
    assert checkpoint.observed_context.captured_at_s == 100.36
    assert checkpoint.observed_context.schema_version == 2
    assert not any(
        (
            checkpoint.observed_context.map_cache_available,
            checkpoint.observed_context.dem_available,
            checkpoint.observed_context.geofence_valid,
            checkpoint.observed_context.perception_available,
        )
    )
    assert not capture._closed and receiver.to_mapping()["thread_alive"]
    report = receipt(collection_fixture)
    assert report["collection_complete"] and report["checkpoint_qualified"]
    assert report["worker_exited"] and not any(report["claims"].values())
    assert not report["writer_sealed"] and not report["RX_restarted"]
    assert report["parameter_baseline"] == args["parameter_baseline"]


def test_second_new_checkpoint_reuses_original_baseline_without_renewal(collection_fixture):
    first = call(collection_fixture)
    baseline = first.to_mapping()["inputs"]["parameter_baseline"]
    second = call(collection_fixture, "second", parameter_baseline=baseline)
    assert second.to_mapping()["inputs"]["parameter_baseline"] == baseline
    assert receipt(collection_fixture, "second")["baseline_reused"]
    assert baseline["started_at_s"] == 99 and baseline["finished_at_s"] == 99.5


def test_stale_oldest_source_refused_and_receipt_keeps_native_inputs(collection_fixture):
    collection_fixture[0].clock.value = 103
    with pytest.raises(ValueError):
        call(collection_fixture)
    report = receipt(collection_fixture)
    assert not report["checkpoint_qualified"] and report["error"]
    assert report["collection_complete"] and report["native_rows"]
    assert not collection_fixture[2]._closed
    assert collection_fixture[1].to_mapping()["thread_alive"]


def test_parameter_baseline_tamper_not_repaired_or_refreshed(collection_fixture):
    baseline = copy.deepcopy(collection_fixture[4]["parameter_baseline"])
    source = baseline["streams"]["stdout"]["original_bytes"]
    path = Path(source["path"])
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ValueError):
        call(collection_fixture, parameter_baseline=baseline)
    report = receipt(collection_fixture)
    assert report["parameter_baseline"] == baseline and not report["checkpoint_qualified"]


@pytest.mark.parametrize("change", ["closed", "foreign-RX", "foreign-clock", "capacity", "camera"])
def test_initial_open_scope_refusal(collection_fixture, change):
    session, receiver, capture, _, _, _ = collection_fixture
    if change == "closed":
        session._closed = True
    elif change == "foreign-RX":
        session.receiver = object()
    elif change == "foreign-clock":
        capture.clock_id = "foreign"
    elif change == "capacity":
        capture.max_records = 20000
    else:
        session._active_camera = object()
    with pytest.raises(ValueError):
        call(collection_fixture)
    assert not (collection_fixture[4]["artifact_root"] / "collected").exists()


def test_source_failure_does_not_seal_or_stop_original_RX(collection_fixture, monkeypatch):
    real_owner = provider._SourceOwner

    class FailingOwner(real_owner):
        def _run(self):
            self.failure = RuntimeError("actual source failed CPU seam")
            self.done.set()

    monkeypatch.setattr(provider, "_SourceOwner", FailingOwner)
    with pytest.raises(RuntimeError, match="source failed"):
        call(collection_fixture)
    report = receipt(collection_fixture)
    assert not report["collection_complete"] and not report["checkpoint_qualified"]
    assert report["worker_exited"] and not collection_fixture[2]._closed
    assert collection_fixture[1].to_mapping()["thread_alive"]


def native_result(raw=b"source\n", *, exit_code=0, stderr_complete=True):
    def stream(raw, complete):
        return {
            "prefix": raw,
            "observed_bytes": len(raw),
            "EOF": True,
            "error": None,
            "all_bytes_retained": complete,
            "full_output_sha256": hashlib.sha256(raw).hexdigest(),
        }

    return {
        "native_exit_code": exit_code,
        "owned_process_exited": True,
        "reader_threads_exited": True,
        "complete_output_observed": True,
        "timeout": False,
        "streams": {"stdout": stream(raw, True), "stderr": stream(b"", stderr_complete)},
    }


@pytest.mark.parametrize("failure", [None, "native-exit", "stderr-incomplete", "stdout-hash"])
def test_native_original_bytes_and_complete_both_streams(tmp_path, monkeypatch, failure):
    clock = helpers.Clock()
    session = SimpleNamespace(
        clock=clock,
        _closed=False,
        _failure=None,
        container_id="1" * 64,
        plan=SimpleNamespace(docker_plan=SimpleNamespace(docker_executable="docker")),
        _remaining=lambda: 10,
    )
    result = native_result(
        exit_code=1 if failure == "native-exit" else 0,
        stderr_complete=failure != "stderr-incomplete",
    )
    if failure == "stdout-hash":
        result["streams"]["stdout"]["full_output_sha256"] = "0" * 64
    commands = []

    def runner(command, **kwargs):
        commands.append((command, kwargs))
        return result

    monkeypatch.setattr(provider, "capture_native_prefix", runner)
    owner = provider._SourceOwner(session, tmp_path, 110, None)
    if failure is None:
        row = owner._native("navigation-before")
        assert row["error"] is None
    else:
        with pytest.raises(ValueError):
            owner._native("navigation-before")
    retained = json.loads((tmp_path / "navigation-before-result.json").read_bytes())
    assert retained["API_attempted"] and retained["API_returned"]
    assert (tmp_path / "navigation-before.stdout.bin").read_bytes() == b"source\n"
    assert (tmp_path / "navigation-before.stderr.bin").read_bytes() == b""
    assert commands[0][0] == [
        "docker",
        "exec",
        "1" * 64,
        *provider.navigation_reference_phase_argv(),
    ]
    assert not any(retained["claims"].values())


def test_allowlist_does_not_accept_unknown_native_command(tmp_path):
    session = SimpleNamespace(
        plan=SimpleNamespace(docker_plan=SimpleNamespace(docker_executable="docker")),
        container_id="1" * 64,
    )
    owner = provider._SourceOwner(session, tmp_path, 110, None)
    with pytest.raises(ValueError, match="fixed readonly"):
        owner._command("arm")


def test_current_scope_rejects_mid_collection_RX_replacement(collection_fixture):
    session, receiver, capture, _, _, _ = collection_fixture
    session.receiver = object()
    with pytest.raises(ValueError, match="identity changed"):
        provider._check_open_current(session, receiver, capture, 120)


def test_owner_worker_fixed_stage_order(tmp_path, monkeypatch):
    session = SimpleNamespace()
    owner = provider._SourceOwner(session, tmp_path, 110, None)
    stages = []

    def native(name):
        stages.append(name)
        return {"name": name}

    monkeypatch.setattr(owner, "_native", native)
    monkeypatch.setattr(owner, "_camera", lambda: stages.append("camera-two-fresh-frames"))
    owner._run()
    assert owner.failure is None and owner.done.is_set()
    assert stages == [
        "parameter-baseline",
        "loaded-world-sdf",
        "owned-inspect-before",
        "navigation-before",
        "camera-two-fresh-frames",
        "navigation-after",
        "owned-inspect-after",
    ]


@pytest.mark.parametrize("failure", [None, "read", "close"])
def test_scoped_two_frame_camera_close_and_original_stamps(tmp_path, monkeypatch, failure):
    # Camera lifecycle seam only. These objects are never passed to a qualifier.
    clock = helpers.Clock()
    calls = []
    backend = SimpleNamespace(close=lambda: calls.append("backend-close"))
    session = SimpleNamespace(
        clock=clock,
        _closed=False,
        _failure=None,
        _remaining=lambda: 10,
        plan=SimpleNamespace(frame_count=2),
        container_id="1" * 64,
        _active_camera=None,
        _camera_lock=threading.Lock(),
        _camera=lambda *_: backend,
    )

    class CameraSeam:
        def __init__(self, actual_backend, plan, actual_clock, deadline):
            assert actual_backend is backend and actual_clock is clock
            self.source = SimpleNamespace(stats=SimpleNamespace(to_mapping=lambda: {"CPU": True}))
            self.count = 0

        def read(self, *, deadline_s):
            calls.append(("read", deadline_s))
            if failure == "read":
                raise RuntimeError("camera read failure CPU seam")
            self.count += 1
            return SimpleNamespace(
                host_return_stamp=SimpleNamespace(value_ns=100_100_000_000 + self.count),
                provenance=SimpleNamespace(sensor_stamp_ns=10_100_000_000 + self.count),
            )

        def close(self, *, require_complete):
            calls.append(("close", require_complete))
            if failure == "close":
                raise RuntimeError("camera close failure CPU seam")

    monkeypatch.setattr(provider, "BoundedTeacherCamera", CameraSeam)
    monkeypatch.setattr(
        provider,
        "_persist_camera",
        lambda directory, observation: {
            "path": str(directory / "CPU-camera.json"),
            "bytes": 1,
            "sha256": "0" * 64,
        },
    )
    owner = provider._SourceOwner(session, tmp_path, 110, None)
    if failure is None:
        owner._camera()
        assert len(owner.camera_updates) == 2
        assert [row["frame_index"] for row in owner.camera_updates] == [0, 1]
        assert owner.camera_updates[0]["source_sensor_stamp_ns"] == 10_100_000_001
        assert owner.camera_updates[0]["host_return_at_s"] == 100.100000001
        assert [call for call in calls if isinstance(call, tuple) and call[0] == "read"] == [
            ("read", 110),
            ("read", 110),
        ]
    else:
        with pytest.raises(RuntimeError, match="camera"):
            owner._camera()
    assert session._active_camera is None
    assert any(isinstance(call, tuple) and call[0] == "close" for call in calls)


def test_output_creation_source_failure_retains_diagnostic_only(collection_fixture):
    source = collection_fixture[5]["world_source"]
    path = collection_fixture[4]["artifact_root"] / source.path
    path.write_bytes(path.read_bytes() + b"modified")
    with pytest.raises(RuntimeError, match="source drifted"):
        call(collection_fixture)
    report = receipt(collection_fixture)
    assert not report["collection_complete"] and not report["checkpoint_qualified"]
    assert not report["worker_exited"]  # No source worker was ever constructed.
    assert not collection_fixture[2]._closed

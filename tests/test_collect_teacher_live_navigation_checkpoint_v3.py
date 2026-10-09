"""V4 provider CPU seams + original wire checks; NOT actual SIM evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import collect_teacher_live_navigation_checkpoint_v3 as provider
from scripts.onboard_teacher_sortie_artifact_ledger_v1 import TeacherSortieArtifactLedger

_SPEC = importlib.util.spec_from_file_location(
    "_V4_provider_checkpoint_fixture",
    Path(__file__).with_name("test_teacher_live_navigation_checkpoint_v2.py"),
)
helpers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(helpers)


@pytest.fixture
def provider_fixture(tmp_path, monkeypatch):
    generator = helpers.live_v4_fixture.__wrapped__(tmp_path)
    args, capture, clock, budget = next(generator)
    source_api = provider.source_api
    plan = object.__new__(source_api.TeacherSessionPlan)
    for name, value in {
        "frame_count": 2,
        "camera_timeout_s": 1,
        "session_timeout_s": 235,
        "width": 2,
        "height": 2,
        "source_manifest_path": tmp_path / "fixture.scenario.json",
        "evidence_dir": tmp_path / "CPU-session",
        "docker_plan": SimpleNamespace(
            docker_executable="docker",
            world_path=tmp_path / args["world_source"].path,
            to_mapping_with_hash=lambda: args["plan"]["docker_plan"],
        ),
    }.items():
        object.__setattr__(plan, name, value)
    session = object.__new__(source_api.OwnedTeacherGazeboSessionV3)
    session.plan, session.clock, session._owner = plan, clock, threading.get_ident()
    session._entered, session.execute = True, True
    session._closed, session._failure = False, None
    session.deadline_s = budget.deadline_s
    session.container_id, session.owner_token = args["container_id"], args["owner_token"]
    session._active_camera, session._camera_thread = None, None
    session._camera_lock = threading.Lock()

    def empty_reader(_):
        threading.Event().wait(0.002)
        return None

    receiver = source_api.ConcurrentTelemetryReceiver(
        empty_reader,
        clock.monotonic,
        queue_capacity=budget.queue_capacity,
    )
    receiver.start()
    session.receiver = receiver

    class OriginalCPUOwner(source_api._SourceOwner):
        def _run(self):
            self.rows = {
                name.replace("_", "-"): copy.deepcopy(args[name.replace("-", "_")])
                for name in (
                    "navigation_before",
                    "navigation_after",
                    "owned_inspect_before",
                    "owned_inspect_after",
                )
            }
            self.baseline = self.baseline or copy.deepcopy(args["parameter_baseline"])
            self.camera_updates = copy.deepcopy(args["camera_updates"])
            self.done.set()

    monkeypatch.setattr(source_api, "_SourceOwner", OriginalCPUOwner)
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


def call(fixture, output="V4-collected", **updates):
    session, receiver, capture, plan, args, inputs = fixture
    return provider.collect_live_checkpoint(
        session,
        receiver,
        capture,
        plan,
        args["artifact_root"],
        args["artifact_root"] / output,
        **{**inputs, **updates},
    )


def test_exact_V4_provider_to_typed_budget_bound_checkpoint(provider_fixture):
    session, receiver, capture, _, _, _ = provider_fixture
    checkpoint = call(provider_fixture)
    assert type(checkpoint) is provider.checkpoint_api.LiveTeacherNavigationCheckpointV2
    assert checkpoint._live_capture is capture and checkpoint._live_clock is session.clock
    assert checkpoint.budget.canonical_hash == capture.budget.canonical_hash
    assert checkpoint.observed_context.captured_at_s == 100.36
    assert not capture._closed and receiver.to_mapping()["thread_alive"]
    assert not checkpoint.observed_context.geofence_valid


def test_default_world_SDF_uses_exact_whole_file_pointer_None(provider_fixture):
    checkpoint = call(provider_fixture, world_source=None)
    assert checkpoint.world_source.json_pointer is None
    assert checkpoint.world_source.sha256 == provider_fixture[5]["world_source"].sha256


def test_JSON_pointer_cannot_be_misused_for_SDF(provider_fixture):
    world = replace(provider_fixture[5]["world_source"], json_pointer="")
    with pytest.raises(ValueError, match="whole-file"):
        call(provider_fixture, world_source=world)


def test_declared_queue_allocation_must_match_actual_RX(provider_fixture):
    provider_fixture[1].capacity = 256
    with pytest.raises(ValueError, match="capacity differs"):
        call(provider_fixture)


def test_once_bound_phase_deadline_not_renewed(provider_fixture):
    session, _, capture, _, _, _ = provider_fixture
    session.clock.value = capture.budget.phase_deadline_s("preflight")
    with pytest.raises(ValueError, match="phase deadline"):
        call(provider_fixture)


def test_model_partial_JSON_binding_refused_before_native_worker(provider_fixture):
    models = dict(provider_fixture[5]["model_sources"])
    key = next(iter(models))
    models[key] = replace(models[key], json_pointer="")
    with pytest.raises(ValueError, match="whole-file"):
        call(provider_fixture, model_sources=models)


def test_old_actual_provider_sources_remain_unchanged():
    root = Path(__file__).resolve().parents[1]
    expected = {
        "collect_teacher_live_navigation_checkpoint_v1.py": (
            "96628a2838cf87452be676421e606f28524f494f89af723730aa27fa261cf65f"
        ),
        "collect_teacher_live_navigation_checkpoint_v2.py": (
            "50fb0190585cda838bb815a4e4adf37614b05584f8e0d7d21d543358bc0bec21"
        ),
    }
    for name, digest in expected.items():
        assert hashlib.sha256((root / "scripts" / name).read_bytes()).hexdigest() == digest


def test_actual_collection_report_write_time_remains_inside_original_TTL(
    provider_fixture, monkeypatch
):
    session, _, _, _, args, _ = provider_fixture
    save = provider.checkpoint_api._save_json

    def slow_report(root, path, value, ledger=None):
        result = save(root, path, value, ledger)
        if path.name == "collection.json":
            session.clock.value = 103
        return result

    monkeypatch.setattr(provider.checkpoint_api, "_save_json", slow_report)
    with pytest.raises(ValueError, match="expired during collection receipt"):
        call(provider_fixture)
    report = json.loads((args["artifact_root"] / "V4-collected/collection.json").read_bytes())
    # This receipt records semantic completion before its own write; it is
    # never a flight/current-lease authorization at the return clock.
    assert report["checkpoint_qualified"] and not any(report["claims"].values())


def ledger_fixture(fixture, name="worker-ledger"):
    session, _, capture, plan, args, _ = fixture
    root = args["artifact_root"]
    ledger = TeacherSortieArtifactLedger(
        budget=replace(capture.budget, evidence_bytes=16 * 1024 * 1024),
        clock=session.clock.monotonic,
        camera_directory=root / f"{name}-camera",
        evidence_directory=root / f"{name}-evidence",
    )
    sources, cameras = ledger.evidence_directory / "sources", ledger.camera_directory / "sources"
    sources.mkdir()
    cameras.mkdir()
    reservations = provider._prepare_source_reservations(ledger, sources, cameras, plan, None)
    return ledger, sources, cameras, reservations


def test_all_native_and_paired_camera_paths_precredited_before_worker(provider_fixture):
    ledger, sources, cameras, reservations = ledger_fixture(provider_fixture)
    try:
        assert len(reservations) == 6 * 4 + 2 * 3
        assert all(not Path(ticket.path).exists() for ticket in reservations.values())
        rows = [json.loads(raw) for raw in ledger._journal_path.read_bytes().splitlines()]
        assert len([row for row in rows if row.get("event") == "reserved-before-write"]) == 30
        expected = {
            "evidence": 6 * (2 * 65536 + 2 * provider.source_api.NATIVE_LIMIT_BYTES),
            "camera": 2 * (2 * 2 * 7 + 128 * 1024),
        }
        for kind, total in expected.items():
            assert (
                sum(r.maximum_bytes for r in reservations.values() if r.category == kind) == total
            )
        assert not (sources / "loaded-world-sdf.stdout.bin").exists()
        assert not (cameras / "camera-000/depth.bin").exists()
        with pytest.raises(ValueError, match="Pending/partial"):
            ledger.capacity()
    finally:
        ledger.close()


def test_real_native_stream_files_commit_zero_stderr_without_refund(provider_fixture):
    ledger, _, _, reservations = ledger_fixture(provider_fixture)
    try:
        ticket = next(r for r in reservations.values() if r.path.endswith(".stderr.bin"))
        before = dict(ledger._charged)
        provider.source_api._write_new(Path(ticket.path), b"")
        provider._commit_worker_reservations(ledger, reservations)
        assert ledger._committed[ticket.path][0].bytes == 0
        assert ledger._charged["evidence"] >= before["evidence"]
        assert ticket.index not in ledger._pending and len(ledger._pending) == 29
        # Missing originals are not replaced by bytes from a different source.
        with pytest.raises(ValueError, match="Pending/partial"):
            ledger.capacity()
    finally:
        ledger.close()


def test_frozen_native_byte_helper_writes_only_precredited_paths(provider_fixture, monkeypatch):
    session, _, _, _, _, _ = provider_fixture
    ledger, sources, cameras, reservations = ledger_fixture(provider_fixture)
    owner = provider._PrecreditedSourceOwner(
        session,
        sources,
        110,
        None,
        camera_directory=cameras,
        reservations=reservations,
    )
    calls = []

    def CPU_native_result(command, **limits):
        # No subprocess or Docker here: original local-file persistence is
        # real, while the bounded native API result is explicitly a CPU seam.
        assert (sources / "loaded-world-sdf-attempt.json").is_file()
        assert all(
            (sources / filename).as_posix() in {r.path for r in ledger._pending.values()}
            for filename in (
                "loaded-world-sdf-attempt.json",
                "loaded-world-sdf-result.json",
                "loaded-world-sdf.stdout.bin",
                "loaded-world-sdf.stderr.bin",
            )
        )
        calls.append(command)
        streams = {}
        for label, raw in (("stdout", b"<CPU-native-seam/>"), ("stderr", b"")):
            streams[label] = {
                "prefix": raw,
                "observed_bytes": len(raw),
                "all_bytes_retained": True,
                "full_output_sha256": hashlib.sha256(raw).hexdigest(),
                "EOF": True,
                "error": None,
            }
        return {
            "native_exit_code": 0,
            "owned_process_id": 1,
            "owned_process_exited": True,
            "reader_threads_exited": True,
            "complete_output_observed": True,
            "timeout": False,
            "output_limit_bytes": limits["output_limit_bytes"],
            "prefix_limit_bytes_per_stream": limits["prefix_bytes"],
            "streams": streams,
        }

    monkeypatch.setattr(provider.source_api, "capture_native_prefix", CPU_native_result)
    try:
        result = owner._native("loaded-world-sdf")
        provider._commit_worker_reservations(ledger, reservations)
        assert len(calls) == 1 and result["native_exit_code"] == 0
        stdout = result["streams"]["stdout"]["original_bytes"]
        stderr = result["streams"]["stderr"]["original_bytes"]
        assert stdout["bytes"] == len(b"<CPU-native-seam/>") and stderr["bytes"] == 0
        assert len(ledger._pending) == 26
        for suffix in ("-attempt.json", "-result.json", ".stdout.bin", ".stderr.bin"):
            path = (sources / f"loaded-world-sdf{suffix}").as_posix()
            assert path in ledger._committed
    finally:
        ledger.close()


def paired_CPU_observation(index, *, oversized=False):
    rgb = bytes((index, 1, 2)) * (5 if oversized else 4)
    depth = b"\x00\x00\x80\x3f" * 4
    return SimpleNamespace(
        rgb_bgr24=rgb,
        depth=SimpleNamespace(payload=depth),
        host_return_stamp=SimpleNamespace(value_ns=int((100.3 + index * 0.1) * 1e9)),
        provenance=SimpleNamespace(sensor_stamp_ns=int((10.1 + index * 0.1) * 1e9)),
        to_mapping=lambda: {
            "CPU_fixture_only": True,
            "rgb_bgr24_sha256": hashlib.sha256(rgb).hexdigest(),
            "depth": {"payload_sha256": hashlib.sha256(depth).hexdigest()},
        },
    )


def install_CPU_camera(session, monkeypatch, *, oversized=False):
    class CPUBackend:
        def close(self):
            pass

    class CPUBoundedCamera:
        def __init__(self, *_):
            self.index = 0
            self.source = SimpleNamespace(
                stats=SimpleNamespace(to_mapping=lambda: {"CPU_only": True})
            )

        def read(self, **_):
            result = paired_CPU_observation(self.index, oversized=oversized)
            self.index += 1
            return result

        def close(self, **_):
            pass

    session._camera = lambda *_: CPUBackend()
    monkeypatch.setattr(provider.source_api, "BoundedTeacherCamera", CPUBoundedCamera)


def test_worker_camera_files_physically_respect_main_precredit(provider_fixture, monkeypatch):
    session, _, _, _, args, _ = provider_fixture
    ledger, sources, cameras, reservations = ledger_fixture(provider_fixture)
    install_CPU_camera(session, monkeypatch)
    owner = provider._PrecreditedSourceOwner(
        session,
        sources,
        110,
        None,
        camera_directory=cameras,
        reservations=reservations,
    )
    try:
        # Exercise a real distinct worker thread: no ledger API or main writer
        # ownership is delegated to the native/camera owner.
        errors = []

        def run():
            try:
                owner._camera()
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive() and not errors and len(owner.camera_updates) == 2
        provider._commit_worker_reservations(ledger, reservations)
        for update in owner.camera_updates:
            from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding

            camera_source = provider.source_api._source(
                Path(update["camera"]["path"]), args["artifact_root"]
            )
            ledger.require_source(
                camera_source, artifact_root=args["artifact_root"], category="camera"
            )
            metadata = json.loads(Path(update["camera"]["path"]).read_bytes())
            assert metadata["CPU_fixture_only"] and not any(metadata["claims"].values())
            for raw in metadata["persisted_payloads"].values():
                relative = dict(
                    raw, path=Path(raw["path"]).relative_to(args["artifact_root"]).as_posix()
                )
                ledger.require_source(
                    EvidenceBinding(
                        path=relative["path"],
                        bytes=relative["bytes"],
                        sha256=relative["sha256"],
                        source_id="CPU-camera-original",
                        json_pointer=None,
                    ),
                    artifact_root=args["artifact_root"],
                    category="camera",
                )
        assert session._active_camera is None
        assert len(ledger._pending) == 24  # Native sources were NOT collected by this CPU seam.
    finally:
        ledger.close()


def test_oversized_camera_refuses_before_any_payload_write(provider_fixture, monkeypatch):
    session, _, _, _, _, _ = provider_fixture
    ledger, sources, cameras, reservations = ledger_fixture(provider_fixture)
    install_CPU_camera(session, monkeypatch, oversized=True)
    owner = provider._PrecreditedSourceOwner(
        session,
        sources,
        110,
        None,
        camera_directory=cameras,
        reservations=reservations,
    )
    try:
        with pytest.raises(ValueError, match="prior physical reservation"):
            owner._camera()
        assert not (cameras / "camera-000/rgb-bgr24.bin").exists()
        assert not owner.camera_updates and session._active_camera is None
        assert len(ledger._pending) == 30
    finally:
        ledger.close()


def test_native_metadata_bound_refuses_before_attempt_write(provider_fixture, monkeypatch):
    session, _, _, _, _, _ = provider_fixture
    ledger, sources, cameras, reservations = ledger_fixture(provider_fixture)
    owner = provider._PrecreditedSourceOwner(
        session,
        sources,
        110,
        None,
        camera_directory=cameras,
        reservations=reservations,
    )
    monkeypatch.setattr(owner, "_command", lambda _: ["x" * 8193])
    try:
        with pytest.raises(ValueError, match="pre-write envelope"):
            owner._native("loaded-world-sdf")
        assert not (sources / "loaded-world-sdf-attempt.json").exists()
    finally:
        ledger.close()

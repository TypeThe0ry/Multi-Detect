"""CPU-only ownership/journaling fixtures, not native model or Gazebo evidence."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_onboard_navigation_gazebo_learned_shadow as live
from scripts.onboard_navigation_inference_backend import NavigationBackendRejected


def request(generation=1, sha="a" * 64):
    return SimpleNamespace(
        request=SimpleNamespace(
            generation=generation,
            canonical_hash=sha,
            actor_identity_sha256="b" * 64,
            source_binding_sha256="c" * 64,
            source_frames=(object(),),
            submitted_at_s=1,
            deadline_s=6,
            max_frame_age_s=5,
        )
    )


def test_rejected_actual_raw_is_durable_and_not_replaced(tmp_path):
    class Reject:
        def infer(self, value):
            raise NavigationBackendRejected(
                "negative learned distance",
                {
                    "raw_prediction": {"distance_m": -2, "yaw_rad": 0.1},
                    "fused_forward_returned": True,
                },
            )

    sink = live.ShadowRequestSink(Reject(), tmp_path / "raw.jsonl", clock=lambda: 2)
    sink(request())
    row = json.loads((tmp_path / "raw.jsonl").read_text())
    assert row["accepted"] is False
    assert row["raw_prediction"]["distance_m"] == -2
    assert row["flight_commands_sent"] == 0
    assert row["retry_performed"] is False
    assert row == sink.rows[0]
    with pytest.raises(ValueError, match="Duplicate"):
        sink(request())
    assert len((tmp_path / "raw.jsonl").read_text().splitlines()) == 1


def test_accepted_and_rejected_journal_preserves_generation_order(tmp_path):
    class Backend:
        def infer(self, value):
            if value.generation == 2:
                raise NavigationBackendRejected("expired", {"raw_prediction": None})
            return {
                "learned_readout_result": SimpleNamespace(to_mapping=lambda: {"target": "stop"}),
                "raw_prediction": {"kind": "sequence_stop"},
                "metrics": {"result_accepted": True},
            }

    sink = live.ShadowRequestSink(Backend(), tmp_path / "raw.jsonl", clock=lambda: 2)
    sink(request())
    sink(request(2, "d" * 64))
    rows = [json.loads(line) for line in (tmp_path / "raw.jsonl").read_text().splitlines()]
    assert [row["generation"] for row in rows] == [1, 2]
    assert [row["accepted"] for row in rows] == [True, False]
    assert all(row["flight_commands_sent"] == 0 for row in rows)
    assert rows[0]["result"] == {"target": "stop"}


@pytest.mark.parametrize("generation", [0, 2, 100])
def test_generation_gaps_never_call_backend(tmp_path, generation):
    backend = SimpleNamespace(infer=lambda _: pytest.fail("must reject before inference"))
    sink = live.ShadowRequestSink(backend, tmp_path / "raw.jsonl", clock=lambda: 2)
    with pytest.raises(ValueError, match="generations"):
        sink(request(generation))
    assert not sink.journal.exists()


def test_no_swallowing_unexpected_backend_crash(tmp_path):
    def crash(_):
        raise RuntimeError("native forward crashed")

    sink = live.ShadowRequestSink(
        SimpleNamespace(infer=crash), tmp_path / "raw.jsonl", clock=lambda: 2
    )
    with pytest.raises(RuntimeError, match="crashed"):
        sink(request())
    assert sink.rows == []
    assert not sink.journal.exists()


def test_cap_prevents_additional_inference(tmp_path):
    class Backend:
        def infer(self, value):
            raise NavigationBackendRejected("fixture rejection", {})

    sink = live.ShadowRequestSink(
        Backend(), tmp_path / "raw.jsonl", clock=lambda: 2, max_requests=1
    )
    sink(request())
    with pytest.raises(ValueError, match="excess"):
        sink(request(2, "d" * 64))


def test_existing_journal_is_never_overwritten(tmp_path):
    path = tmp_path / "raw.jsonl"
    path.write_text("user\n")
    backend = SimpleNamespace(
        infer=lambda _: (_ for _ in ()).throw(NavigationBackendRejected("bad", {}))
    )
    sink = live.ShadowRequestSink(backend, path, clock=lambda: 2)
    with pytest.raises(FileExistsError):
        sink(request())
    assert path.read_text() == "user\n"


def test_only_successful_exact_owned_docker_launch_is_claimed(tmp_path, monkeypatch):
    argv = ["docker", "run", "--name", "owned", "image"]
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="f" * 64 + "\n"),
    )
    run = live.owned_docker_runner(tmp_path / "claim.json", "owned")
    result = run(argv, timeout_s=5)
    assert result.returncode == 0
    claim = json.loads((tmp_path / "claim.json").read_text())
    assert claim["container_id"] == "f" * 64
    assert claim["launch_command"] == argv
    with pytest.raises(FileExistsError):
        run(argv, timeout_s=5)


@pytest.mark.parametrize("stdout,name", [("bogus", "owned"), ("f" * 64, "user-container")])
def test_malformed_or_foreign_launch_not_claimed(tmp_path, monkeypatch, stdout, name):
    monkeypatch.setattr(
        live.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout)
    )
    with pytest.raises(ValueError):
        live.owned_docker_runner(tmp_path / "claim.json", "owned")(
            ["docker", "run", "--name", name, "image"], timeout_s=5
        )
    assert not (tmp_path / "claim.json").exists()


def fake_runtime(tmp_path):
    packages, files, origins = {}, [], {}
    for name, version in live.PACKAGE_VERSIONS.items():
        root = tmp_path / name
        root.mkdir()
        (root / "__init__.py").write_text("fixture = True\n")
        rows = [live.record(root / "__init__.py")]
        files.extend(rows)
        origins[name] = (root / "__init__.py").as_posix()
        packages[name] = {"version": version, "root": root.as_posix(), "files": rows}
    return {
        "contract": live.CAPTURE_CONTRACT,
        "python_executable": Path(sys.executable).resolve().as_posix(),
        "provider_site": tmp_path.as_posix(),
        "packages": packages,
        "source_files": files,
        "module_origins": origins,
        "no_sys_path_insertion": True,
        "allowed_fallback_top_level_names": sorted(live.PACKAGE_VERSIONS),
        "shadow_existing_modules": False,
    }


def test_package_finder_never_handles_ml_or_nested_modules(tmp_path):
    finder = live._CapturePackageFinder(fake_runtime(tmp_path))
    assert finder.find_spec("torch") is None
    assert finder.find_spec("numpy") is None
    assert finder.find_spec("pymavlink.mavutil", path=["anything"]) is None
    spec = finder.find_spec("pymavlink")
    assert Path(spec.origin).resolve() == tmp_path / "pymavlink/__init__.py"


def test_dependency_scope_restores_finder_without_sys_path_edits(tmp_path, monkeypatch):
    runtime = fake_runtime(tmp_path)
    original = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name in live.PACKAGE_VERSIONS else original(name),
    )
    before, paths = tuple(sys.meta_path), tuple(sys.path)
    observation = {}
    with pytest.raises(RuntimeError, match="fixture"):
        with live.scoped_capture_dependencies(runtime, observation):
            assert tuple(sys.path) == paths
            raise RuntimeError("fixture")
    assert tuple(sys.meta_path) == before
    assert tuple(sys.path) == paths
    assert observation["fallback_restored"] is True
    assert observation["source_bindings_unchanged"] is True


def test_dependency_scope_rejects_modified_file_before_install(tmp_path, monkeypatch):
    runtime = fake_runtime(tmp_path)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    (tmp_path / "pymavlink/__init__.py").write_text("changed = True\n")
    before = tuple(sys.meta_path)
    with pytest.raises(ValueError, match="files changed"):
        with live.scoped_capture_dependencies(runtime, {}):
            pytest.fail("must fail before installing")
    assert tuple(sys.meta_path) == before


def test_dependency_scope_rejects_shadowing_existing_module(tmp_path, monkeypatch):
    runtime = fake_runtime(tmp_path)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    with pytest.raises(ValueError, match="shadow"):
        with live.scoped_capture_dependencies(runtime, {}):
            pytest.fail("must not shadow")


@pytest.mark.parametrize("match", [True, False])
def test_outer_cleanup_never_deletes_a_different_native_id(tmp_path, monkeypatch, match):
    identity = "a" * 64
    live.write_new(
        tmp_path / "owned-container.json",
        {
            "container_name": "owned",
            "container_id": identity,
        },
    )
    calls = []
    states = iter([{"Id": identity if match else "b" * 64}, None if match else {"Id": "b" * 64}])
    monkeypatch.setattr(live, "docker_inspect", lambda *a: next(states))

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(live.subprocess, "run", run)
    report = {}
    live.observe_owned_cleanup("docker", "owned", tmp_path, report)
    assert len(calls) == (2 if match else 0)
    if match:
        assert calls == [["docker", "stop", "--timeout", "2", identity], ["docker", "rm", identity]]
        assert report["container_absent_after"] is True
    else:
        assert report["container_identity_mismatch_not_touched"] is True
        assert report["container_absent_after"] is False


def test_unclaimed_container_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "docker_inspect", lambda *a: {"Id": "a" * 64})
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: pytest.fail("not authorized"))
    report = {}
    live.observe_owned_cleanup("docker", "owned", tmp_path, report)
    assert report["unclaimed_container_not_touched"] is True
    assert report["container_absent_after"] is False


def test_cleanup_diagnostic_failure_remains_closed_evidence(tmp_path, monkeypatch):
    def unavailable(*_):
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(live, "docker_inspect", unavailable)
    report = {}
    live.observe_owned_cleanup("docker", "owned", tmp_path, report)
    assert report["container_absent_after"] is False
    assert report["outer_cleanup_error"]["message"] == "daemon unavailable"


def test_watchdog_targets_only_its_launched_process_tree(monkeypatch):
    process = SimpleNamespace(pid=1234, poll=lambda: None, wait=lambda **k: -1)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(live.subprocess, "run", run)
    observation = live.terminate_owned_tree(process)
    assert calls == [["taskkill", "/PID", "1234", "/T", "/F"]]
    assert observation["pid"] == 1234


def test_exited_worker_is_never_killed(monkeypatch):
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: pytest.fail("must not kill"))
    assert live.terminate_owned_tree(SimpleNamespace(poll=lambda: 0)) is None


@pytest.mark.parametrize("valid", [True, False])
def test_timeout_launch_window_ownership_recovery_requires_exact_label(
    tmp_path, monkeypatch, valid
):
    output = tmp_path / "run"
    output.mkdir()
    intent = {"container_name": "owned", "image": live.PINNED_IMAGE, "token": "e" * 32}
    live.write_new(tmp_path / "run-ownership.json", intent)
    container = {
        "Id": "f" * 64,
        "Name": "/owned",
        "Config": {
            "Image": live.PINNED_IMAGE,
            "Labels": {live.OWNER_LABEL: "e" * 32 if valid else "d" * 32},
        },
    }
    states = iter([container, None if valid else container])
    monkeypatch.setattr(live, "docker_inspect", lambda *a: next(states))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(live.subprocess, "run", run)
    report = {}
    live.observe_owned_cleanup("docker", "owned", output, report)
    assert len(calls) == (2 if valid else 0)
    if valid:
        recovered = json.loads((output / "owned-container.json").read_text())
        assert recovered["launch_native_exit_code"] is None
        assert recovered["ownership_recovered_from_exact_label_image_name"] is True
        assert report["durable_ownership_recovered"] is True
    else:
        assert not (output / "owned-container.json").exists()
        assert report["unclaimed_container_not_touched"] is True


def test_stop_timeout_still_attempts_matching_id_remove(tmp_path, monkeypatch):
    live.write_new(
        tmp_path / "owned-container.json",
        {
            "container_name": "owned",
            "container_id": "a" * 64,
        },
    )
    states = iter([{"Id": "a" * 64}, None])
    monkeypatch.setattr(live, "docker_inspect", lambda *a: next(states))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "stop":
            raise live.subprocess.TimeoutExpired(argv, 15)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(live.subprocess, "run", run)
    report = {}
    live.observe_owned_cleanup("docker", "owned", tmp_path, report)
    assert len(calls) == 2
    assert report["outer_owned_cleanup_exit_codes"] == [None, 0]
    assert len(report["outer_owned_cleanup_errors"]) == 1
    assert report["container_absent_after"] is True


def test_owned_launch_adds_label_and_native_cidfile_without_changing_image(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="f" * 64 + "\n")

    monkeypatch.setattr(live.subprocess, "run", run)
    intent = {"token": "e" * 32}
    live.owned_docker_runner(tmp_path / "claim.json", "owned", intent)(
        ["docker", "run", "--name", "owned", live.PINNED_IMAGE], timeout_s=5
    )
    actual = calls[0]
    assert actual[2:6] == [
        "--label",
        live.OWNER_LABEL + "=" + "e" * 32,
        "--cidfile",
        str(tmp_path / "native-container.cid"),
    ]
    assert actual[-1] == live.PINNED_IMAGE
    claim = json.loads((tmp_path / "claim.json").read_text())
    assert claim["ownership_token"] == "e" * 32
    assert claim["launch_command"] == actual

"""The path fix must also work outside pytest's repository/src injection."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import run_onboard_navigation_pilot_streaming_no_warmup_importpath as entry

body, sup = entry.body, entry.sup
spec = importlib.util.spec_from_file_location(
    "no_warmup_cpu_fixture",
    Path(__file__).with_name("test_run_onboard_navigation_pilot_streaming_no_warmup.py"),
)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    args = base.invocation.__wrapped__(tmp_path, monkeypatch)
    value = json.loads(args.runtime_manifest.read_bytes())
    value.update(
        contract=entry.CONTRACT,
        entrypoint_sha256=sup.sha256_file(entry.__file__),
        **entry.DECLARATIONS,
    )
    value["source_files"].append(sup.file_record(entry.BODY_SOURCE))
    base.base.write_json(args.runtime_manifest, value)
    args.runtime_manifest_sha256 = sup.sha256_file(args.runtime_manifest)
    return args


def globals_snapshot():
    return (
        body.CONTRACT,
        body.__file__,
        body.validate_manifest,
        body.install_constructor_traceback,
        body.DECLARATIONS,
        *base.globals_snapshot(),
        tuple(sys.path),
    )


def test_parent_preflight_is_stdlib_and_binds_new_entrypoint(invocation):
    before, modules = globals_snapshot(), set(sys.modules)
    with entry.dispatch_scope():
        with body.dispatch_scope(invocation):
            manifest, _, _, records = body.streaming.read_runtime(invocation)
            assert manifest["entrypoint_sha256"] == sup.sha256_file(entry.__file__)
            assert sup.file_record(entry.BODY_SOURCE) in records
            assert sup.file_record(entry.__file__) in records
            assert body.streaming.__file__ == entry.__file__
    assert globals_snapshot() == before
    assert not {"torch", "transformers", "peft", "accelerate"} & (set(sys.modules) - modules)


@pytest.mark.parametrize(
    "kind", ["r1-missing", "r1-drift", "entrypoint", "policy", "scope", "path"]
)
def test_source_or_scope_mismatch_rejected_before_writes(invocation, monkeypatch, kind):
    value = json.loads(invocation.runtime_manifest.read_bytes())
    if kind == "r1-missing":
        value["source_files"] = [
            r for r in value["source_files"] if r["path"] != str(entry.BODY_SOURCE)
        ]
    elif kind == "r1-drift":
        drift = invocation.plan.parent / "drifted-r1.py"
        drift.write_bytes(b"CPU fixture identity drift, not sealed source")
        monkeypatch.setattr(entry, "BODY_SOURCE", drift)
        value["source_files"].append(sup.file_record(drift))
    elif kind == "entrypoint":
        value["entrypoint_sha256"] = "0" * 64
    else:
        value[
            {
                "policy": "constructor_importpath_policy",
                "scope": "constructor_importpath_scope",
                "path": "constructor_importpath",
            }[kind]
        ] = "different"
    base.base.write_json(invocation.runtime_manifest, value)
    invocation.runtime_manifest_sha256 = sup.sha256_file(invocation.runtime_manifest)
    before = globals_snapshot()
    with pytest.raises(ValueError):
        entry.supervise(invocation)
    assert globals_snapshot() == before
    assert not invocation.output_dir.exists() and not invocation.supervisor_dir.exists()


@pytest.mark.parametrize("which", ["parent", "worker"])
def test_existing_directory_is_untouched(invocation, which):
    invocation.output_dir.mkdir()
    marker = invocation.output_dir / "marker"
    marker.write_bytes(b"user bytes")
    before = globals_snapshot()
    with pytest.raises(FileExistsError):
        entry.worker(invocation) if which == "worker" else entry.supervise(invocation)
    assert list(invocation.output_dir.iterdir()) == [marker]
    assert marker.read_bytes() == b"user bytes"
    assert globals_snapshot() == before


def test_scoped_constructor_path_and_all_dispatch_hooks_restore_on_error(monkeypatch):
    def original_constructor(manifest):
        assert sys.path[0] == str(entry.SRC)
        sys.path.append("CPU fixture constructor modification")
        raise RuntimeError("original hook failed")

    monkeypatch.setattr(body, "install_constructor_traceback", original_constructor)
    before = globals_snapshot()
    with pytest.raises(RuntimeError, match="original hook failed"):
        with entry.dispatch_scope():
            body.install_constructor_traceback({})
    assert globals_snapshot() == before


def test_nested_dispatch_and_sys_path_restore():
    before = globals_snapshot()
    with entry.dispatch_scope():
        outer = globals_snapshot()
        with entry.dispatch_scope():
            sys.path.append("temporary nested CPU entry")
        assert globals_snapshot() == outer
    assert globals_snapshot() == before


def test_real_isolated_subprocess_reproduces_then_fixes_exact_trainer_import(tmp_path):
    # -I -S strips PYTHONPATH/sitecustomize/pytest injection; no Torch is imported.
    code = r"""
import importlib, json, runpy, sys
from pathlib import Path
script = Path(sys.argv[1]).resolve()
namespace = runpy.run_path(str(script), run_name="isolated_import_fixture")
scope = namespace["dispatch_scope"]
body = namespace["body"]
src = namespace["SRC"]
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != src]
initial = sys.path.copy()
try:
    importlib.import_module("scripts.train_onboard_vlm_navigation_pilot")
except ModuleNotFoundError as exc:
    assert exc.name == "multidetect", exc
else:
    raise AssertionError("Isolated process must reproduce the original missing-src import")
original = body.install_constructor_traceback
calls = []
def stub_original_constructor(manifest):
    assert sys.path[0] == str(src)
    module = importlib.import_module("scripts.train_onboard_vlm_navigation_pilot")
    assert Path(module.__file__).resolve() == body.TRAINER_SOURCE
    assert module.NavigationBackend.__module__ == module.__name__
    assert "torch" not in sys.modules and "transformers" not in sys.modules
    calls.append(module.__name__)
    return {"cpu_stub": True}, lambda: None
body.install_constructor_traceback = stub_original_constructor
try:
    with scope():
        state, restore = body.install_constructor_traceback({})
        assert state == {"cpu_stub": True}
        restore()
        assert sys.path == initial
    assert body.install_constructor_traceback is stub_original_constructor
    assert sys.path == initial and len(calls) == 1
finally:
    body.install_constructor_traceback = original
assert "torch" not in sys.modules and "transformers" not in sys.modules
print(json.dumps({"original_failure_reproduced": True, "src_import_completed": True,
                  "sys_path_restored": True, "heavy_modules_imported": False}))
"""
    completed = subprocess.run(
        [
            str(Path(sys._base_executable).resolve()),
            "-I",
            "-S",
            "-B",
            "-c",
            code,
            str(Path(entry.__file__).resolve()),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result == {
        "original_failure_reproduced": True,
        "src_import_completed": True,
        "sys_path_restored": True,
        "heavy_modules_imported": False,
    }


@pytest.mark.parametrize(
    "mode,bad_journal", [("load-only", False), ("train", False), ("train", True)]
)
def test_new_dispatch_reuses_original_loader_warmup_and_86_gates(
    invocation, monkeypatch, mode, bad_journal
):
    invocation.mode = mode
    popen, commands, calls, mu, original = base.successful_child(
        invocation,
        monkeypatch,
        calls=2 if mode == "train" else 1,
        bad_journal=bad_journal,
    )
    before = globals_snapshot()
    assert entry.supervise(invocation, popen_factory=popen) == int(bad_journal)
    assert globals_snapshot() == before and mu.caching_allocator_warmup is original
    assert commands[0][7] == str(Path(entry.__file__).resolve())
    assert len(calls) == (3 if mode == "train" else 2)
    result = json.loads(
        (invocation.supervisor_dir / "allocator-warmup-completion.json").read_bytes()
    )
    assert result["contract"] == entry.CONTRACT and result["warmup_call_evidence_valid"]
    assert result["training_budget_changed"] is False


def test_child_installer_has_src_before_calling_frozen_ctor_hook(invocation, monkeypatch):
    base.fake_modeling_utils(invocation, monkeypatch)
    original_ctor = body.install_constructor_traceback
    observed = []

    def spy(manifest):
        observed.append(sys.path[0])
        return original_ctor(manifest)

    monkeypatch.setattr(body, "install_constructor_traceback", spy)
    monkeypatch.setattr(body.streaming, "install_streaming_loader", lambda *a: {})

    def stub_worker(args):
        args.output_dir.mkdir()
        body.streaming.install_streaming_loader({}, [])
        sys.modules["scripts.train_onboard_vlm_navigation_pilot"].NavigationBackend(
            "snapshot", "budget"
        )
        return 0

    monkeypatch.setattr(body.streaming, "worker", stub_worker)
    before = globals_snapshot()
    assert entry.worker(invocation) == 0
    assert observed == [str(entry.SRC)]
    assert globals_snapshot() == before


def test_failing_parent_launch_restores_new_dispatch_and_paths(invocation):
    before = globals_snapshot()

    def failed(*args, **kwargs):
        raise OSError("CPU fixture launch error")

    assert entry.supervise(invocation, popen_factory=failed) == 1
    assert globals_snapshot() == before

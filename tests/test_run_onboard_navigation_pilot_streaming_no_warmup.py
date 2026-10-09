"""CPU-only real hook events combined with the unchanged sealed parent gates."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts import run_onboard_navigation_pilot_streaming_no_warmup as entry

streaming, sup = entry.streaming, entry.sup
spec = importlib.util.spec_from_file_location(
    "streaming_cpu_fixture",
    Path(__file__).with_name("test_run_onboard_navigation_pilot_streaming.py"),
)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    args = base.invocation.__wrapped__(tmp_path, monkeypatch)
    path = tmp_path / "new-venv/Lib/site-packages/transformers/modeling_utils.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        '"""CPU identity fixture: no allocator or model implementation."""\n'
        "def caching_allocator_warmup(model, expanded_device_map, hf_quantizer):\n"
        '    raise AssertionError("Original allocator must not execute")\n',
        encoding="utf-8",
    )
    manifest = json.loads(args.runtime_manifest.read_bytes())
    trainer_source = tmp_path / "runtime/cpu_navigation_trainer.py"
    trainer_source.write_text(
        "class NavigationBackend:\n"
        "    def __init__(self, snapshot, budget):\n"
        "        self.snapshot, self.budget = snapshot, budget\n"
        "        if snapshot == 'raise':\n"
        "            raise ORIGINAL_EXCEPTION\n"
        "        warmup(Qwen3VLForConditionalGeneration(), {'x': 'cuda:0'}, None)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(entry, "TRAINER_SOURCE", trainer_source)
    binding = entry.function_binding(path)
    manifest.update(
        contract=entry.CONTRACT,
        entrypoint_sha256=sup.sha256_file(entry.__file__),
        warmup_policy=entry.POLICY,
        warmup_scope=entry.SCOPE,
        allocator_warmup_source=binding["source"],
        allocator_warmup_function_sha256=binding["function_sha256"],
        allocator_warmup_parameter_names=entry.PARAMETERS.copy(),
    )
    manifest["source_files"].extend(
        [sup.file_record(entry.BODY_SOURCE), binding["source"], sup.file_record(trainer_source)]
    )
    base.write_json(args.runtime_manifest, manifest)
    args.runtime_manifest_sha256 = sup.sha256_file(args.runtime_manifest)
    return args


def fake_modeling_utils(args, monkeypatch):
    manifest = json.loads(args.runtime_manifest.read_bytes())
    path = Path(manifest["allocator_warmup_source"]["path"])
    mu = ModuleType("transformers.modeling_utils")
    mu.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mu.__dict__)
    torch = ModuleType("torch")

    class Device:
        def __init__(self, value):
            parts = value.split(":")
            self.type = parts[0]
            self.index = int(parts[1]) if len(parts) == 2 else None

    torch.device = Device
    torch.distributed = SimpleNamespace(is_available=lambda: True, is_initialized=lambda: False)
    monkeypatch.setitem(sys.modules, "transformers.modeling_utils", mu)
    monkeypatch.setitem(sys.modules, "torch", torch)
    trainer = ModuleType("scripts.train_onboard_vlm_navigation_pilot")
    trainer.__file__ = str(entry.TRAINER_SOURCE)
    exec(
        compile(entry.TRAINER_SOURCE.read_text(encoding="utf-8"), trainer.__file__, "exec"),
        trainer.__dict__,
    )
    trainer.warmup = lambda *a: mu.caching_allocator_warmup(*a)
    trainer.Qwen3VLForConditionalGeneration = Qwen3VLForConditionalGeneration
    trainer.ORIGINAL_EXCEPTION = RuntimeError("original constructor failure")
    monkeypatch.setitem(sys.modules, "scripts.train_onboard_vlm_navigation_pilot", trainer)
    return manifest, mu, torch


class Qwen3VLForConditionalGeneration:
    """An empty CPU fixture, not a model or parameter holder."""


def globals_snapshot():
    return (
        streaming.CONTRACT,
        streaming.__file__,
        streaming.read_runtime,
        streaming.install_streaming_loader,
    )


def actual_warmup_trace(args, manifest, mu, *, calls=1):
    stream = io.StringIO()
    original = mu.caching_allocator_warmup
    with contextlib.redirect_stdout(stream):
        state, restore = entry.install_warmup_skip(manifest)
        constructor, restore_constructor = entry.install_constructor_traceback(manifest)
        try:
            for _ in range(calls):
                sys.modules["scripts.train_onboard_vlm_navigation_pilot"].NavigationBackend(
                    "snapshot", "budget"
                )
        finally:
            restore_constructor()
            restore()
        report = {
            "contract": entry.CONTRACT,
            **sup.CLAIMS,
            **entry.DECLARATIONS,
            "mode": args.mode,
            "plan_sha256": args.plan_sha256,
            "runtime_manifest_sha256": args.runtime_manifest_sha256,
            "worker_returncode": 0,
            "warmup_observation": state,
            "constructor_observation": constructor,
        }
        sup.emit("no_warmup_worker_finished", **report)
    assert mu.caching_allocator_warmup is original
    return report, [json.loads(line) for line in stream.getvalue().splitlines()]


def successful_child(args, monkeypatch, *, mutate=None, calls=1, native_exit=0, bad_journal=False):
    manifest, mu, _ = fake_modeling_utils(args, monkeypatch)
    original = mu.caching_allocator_warmup
    parent_popen, commands, loader_calls = base.successful_child(
        args, monkeypatch, native_exit=native_exit
    )

    def popen(command, **kwargs):
        child = parent_popen(command, **kwargs)
        report, warmup_rows = actual_warmup_trace(args, manifest, mu, calls=calls)
        kwargs["stdout"].flush()
        log = args.supervisor_dir / "stdout.bin"
        loader_rows = [json.loads(line) for line in log.read_bytes().splitlines()]
        receipt_path = args.output_dir / "streaming-loader-report.json"
        receipt = json.loads(receipt_path.read_bytes())
        receipt["pid"] = os.getpid()
        for row in loader_rows:
            if row["event"] in ("streaming_runtime_verified", "explicit_streaming_worker_finished"):
                row["pid"] = os.getpid()
        base.write_json(receipt_path, receipt)
        rows = [loader_rows[0], *warmup_rows[:-2], *loader_rows[1:], *warmup_rows[-2:]]
        if mutate is not None:
            mutate(report, rows)
        kwargs["stdout"].seek(0)
        kwargs["stdout"].write(b"\n".join(json.dumps(row).encode() for row in rows) + b"\n")
        kwargs["stdout"].truncate()
        base.write_json(args.output_dir / "allocator-warmup-report.json", report)
        if bad_journal:
            (args.output_dir / "training-events.jsonl").write_bytes(
                b"missing original 86-step events\n"
            )
        return child

    return popen, commands, loader_calls, mu, original


def test_runtime_admission_and_dispatch_are_stdlib_and_restore(invocation):
    before = globals_snapshot()
    modules = set(sys.modules)
    with entry.dispatch_scope(invocation):
        manifest, _, _, records = streaming.read_runtime(invocation)
        assert manifest["contract"] == entry.CONTRACT
        assert sup.file_record(entry.BODY_SOURCE) in records
        assert streaming.__file__ == entry.__file__
        with entry.dispatch_scope(invocation):
            streaming.read_runtime(invocation)
        assert streaming.__file__ == entry.__file__
    assert globals_snapshot() == before
    assert not {"torch", "transformers", "peft", "accelerate"} & (set(sys.modules) - modules)


@pytest.mark.parametrize(
    "kind", ["body", "module", "trainer", "function", "parameters", "policy", "scope"]
)
def test_missing_or_wrong_bindings_rejected_before_output(invocation, kind):
    def mutate(m):
        if kind in ("body", "module", "trainer"):
            selected = {
                "body": str(entry.BODY_SOURCE),
                "module": m["allocator_warmup_source"]["path"],
                "trainer": str(entry.TRAINER_SOURCE),
            }[kind]
            m["source_files"] = [r for r in m["source_files"] if r["path"] != selected]
        elif kind == "function":
            m["allocator_warmup_function_sha256"] = "0" * 64
        elif kind == "parameters":
            m["allocator_warmup_parameter_names"] = ["different"]
        else:
            m["warmup_" + kind] = "different"

    base.mutate_manifest(invocation, mutate)
    before = globals_snapshot()
    with pytest.raises(ValueError):
        entry.supervise(invocation)
    assert globals_snapshot() == before
    assert not invocation.output_dir.exists() and not invocation.supervisor_dir.exists()


@pytest.mark.parametrize("kind", ["existing", "sealed", "runtime", "bad-manifest"])
def test_worker_admission_failure_never_adds_files(invocation, kind):
    if kind == "sealed":
        invocation.output_dir = invocation.plan.parent / "sealed/new-output"
    elif kind == "runtime":
        invocation.output_dir = invocation.runtime_manifest.parent / "new-output"
    else:
        invocation.output_dir.mkdir()
        (invocation.output_dir / "marker").write_bytes(b"user data")
        if kind == "bad-manifest":
            invocation.runtime_manifest_sha256 = "0" * 64
    before = set(invocation.plan.parent.rglob("*"))
    with pytest.raises((ValueError, FileExistsError)):
        entry.worker(invocation)
    assert set(invocation.plan.parent.rglob("*")) == before
    assert not (invocation.output_dir / "allocator-warmup-report.json").exists()


@pytest.mark.parametrize("device", ["cuda:0", 0, "object-cuda", "object-cuda0"])
def test_actual_optional_hook_accepts_only_original_cuda_zero(invocation, monkeypatch, device):
    manifest, mu, torch = fake_modeling_utils(invocation, monkeypatch)
    original = mu.caching_allocator_warmup
    state, restore = entry.install_warmup_skip(manifest)
    selected = (
        torch.device("cuda" if device == "object-cuda" else "cuda:0")
        if str(device).startswith("object")
        else device
    )
    try:
        assert (
            mu.caching_allocator_warmup(Qwen3VLForConditionalGeneration(), {"x": selected}, None)
            is None
        )
    finally:
        restore()
    assert mu.caching_allocator_warmup is original
    assert state["warmup_calls_attempted"] == state["warmup_calls_skipped"] == 1
    assert state["warmup_override_restored"] is True


@pytest.mark.parametrize(
    "kind", ["quantizer", "cpu", "cuda1", "bool", "empty", "model", "distributed"]
)
def test_hook_fail_closed_does_not_allocate_or_change_model(invocation, monkeypatch, kind):
    manifest, mu, torch = fake_modeling_utils(invocation, monkeypatch)
    original = mu.caching_allocator_warmup
    state, restore = entry.install_warmup_skip(manifest)
    model, device_map, quantizer = Qwen3VLForConditionalGeneration(), {"x": "cuda:0"}, None
    if kind == "quantizer":
        quantizer = object()
    elif kind in ("cpu", "cuda1", "bool"):
        device_map["x"] = {"cpu": "cpu", "cuda1": "cuda:1", "bool": False}[kind]
    elif kind == "empty":
        device_map.clear()
    elif kind == "model":
        model = object()
    else:
        torch.distributed.is_initialized = lambda: True
    try:
        with pytest.raises(ValueError):
            mu.caching_allocator_warmup(model, device_map, quantizer)
    finally:
        restore()
    assert mu.caching_allocator_warmup is original
    assert state["warmup_calls_attempted"] == 1 and state["warmup_calls_skipped"] == 0


@pytest.mark.parametrize("kind", ["function", "globals", "source"])
def test_imported_function_identity_is_bound(invocation, monkeypatch, kind):
    manifest, mu, _ = fake_modeling_utils(invocation, monkeypatch)
    if kind == "function":
        mu.caching_allocator_warmup = lambda model, expanded_device_map, hf_quantizer: None
    elif kind == "globals":
        original = mu.caching_allocator_warmup
        from types import FunctionType

        mu.caching_allocator_warmup = FunctionType(original.__code__, {})
    else:
        Path(mu.__file__).write_text(
            "def caching_allocator_warmup(model, expanded_device_map, hf_quantizer):\n"
            "    return None\n",
            encoding="utf-8",
        )
    with pytest.raises((ValueError, OSError)):
        entry.install_warmup_skip(manifest)


def test_load_only_parent_requires_real_hook_and_loader_events(invocation, monkeypatch):
    before = globals_snapshot()
    command_before, capture_before = sup.child_command, sup.capture_sources
    popen, commands, calls, mu, original = successful_child(invocation, monkeypatch)
    assert entry.supervise(invocation, popen_factory=popen) == 0
    assert globals_snapshot() == before and mu.caching_allocator_warmup is original
    assert sup.child_command is command_before and sup.capture_sources is capture_before
    assert commands[0][7] == str(Path(entry.__file__).resolve()) and len(calls) == 2
    result = json.loads(
        (invocation.supervisor_dir / "allocator-warmup-completion.json").read_bytes()
    )
    assert result["warmup_call_evidence_valid"] and result["streaming_supervisor_exit_code"] == 0
    assert all(result[key] is False for key in sup.CLAIMS)
    assert result["training_budget_changed"] is False


@pytest.mark.parametrize("bad_journal", [False, True])
def test_fresh_adapter_get_tensors_reload_keeps_original_86_step_gate(
    invocation, monkeypatch, bad_journal
):
    invocation.mode = "train"
    popen, _, calls, _, _ = successful_child(
        invocation, monkeypatch, calls=2, bad_journal=bad_journal
    )
    assert entry.supervise(invocation, popen_factory=popen) == int(bad_journal)
    assert len(calls) == 3 and calls[-1][1]["backend"] == "mmap"
    result = json.loads(
        (invocation.supervisor_dir / "allocator-warmup-completion.json").read_bytes()
    )
    assert result["warmup_call_evidence_valid"]
    value = json.loads((invocation.supervisor_dir / "completion.json").read_bytes())
    if not bad_journal:
        assert value["optimizer_journal"]["observed_counters"] == dict.fromkeys(sup.COUNTERS, 86)
    else:
        assert not value["child_completion_verified"]


@pytest.mark.parametrize(
    "kind",
    [
        "no-call",
        "missing",
        "pid",
        "source",
        "count",
        "install-flag",
        "restore-flag",
        "final-scope",
        "duplicate-runtime",
        "post-finish",
    ],
)
def test_positive_checkboxes_cannot_replace_actual_call_evidence(invocation, monkeypatch, kind):
    def mutate(report, rows):
        state = report["warmup_observation"]
        selected = next(r for r in rows if r["event"] == "allocator_warmup_skipped")
        if kind == "no-call":
            rows[:] = [r for r in rows if r["event"] != "allocator_warmup_skipped"]
        elif kind == "missing":
            rows[:] = [r for r in rows if not r["event"].startswith("allocator_warmup")]
        elif kind == "pid":
            selected["pid"] += 1
        elif kind == "source":
            selected["allocator_warmup_function_sha256"] = "0" * 64
        elif kind == "count":
            selected["warmup_calls_attempted"] = True
        elif kind == "install-flag":
            next(r for r in rows if r["event"].endswith("override_installed"))[
                "warmup_override_restored"
            ] = True
        elif kind == "restore-flag":
            next(r for r in rows if r["event"].endswith("override_restored"))[
                "warmup_override_installed"
            ] = False
        elif kind == "final-scope":
            state["warmup_scope"] = "all-processes"
        elif kind == "duplicate-runtime":
            rows.insert(1, rows[0].copy())
        else:
            rows.append(selected.copy())

    popen, _, _, _, _ = successful_child(invocation, monkeypatch, mutate=mutate)
    assert entry.supervise(invocation, popen_factory=popen) == 1
    result = json.loads(
        (invocation.supervisor_dir / "allocator-warmup-completion.json").read_bytes()
    )
    assert not result["warmup_call_evidence_valid"]


def test_worker_scopes_restore_after_success_and_exception(invocation, monkeypatch):
    manifest, mu, _ = fake_modeling_utils(invocation, monkeypatch)
    original_mu, before = mu.caching_allocator_warmup, globals_snapshot()
    monkeypatch.setattr(streaming, "install_streaming_loader", lambda *a: {})
    before = globals_snapshot()

    def stub(args):
        args.output_dir.mkdir()
        streaming.install_streaming_loader({}, [])
        sys.modules["scripts.train_onboard_vlm_navigation_pilot"].NavigationBackend(
            "snapshot", "budget"
        )
        raise RuntimeError("CPU fixture worker error after the actual hook")

    monkeypatch.setattr(streaming, "worker", stub)
    assert entry.worker(invocation) == 1
    assert globals_snapshot() == before and mu.caching_allocator_warmup is original_mu
    report = json.loads((invocation.output_dir / "allocator-warmup-report.json").read_bytes())
    assert (
        report["worker_returncode"] == 1
        and report["warmup_observation"]["warmup_override_restored"]
    )
    assert manifest["warmup_scope"] == entry.SCOPE


def test_failed_launch_restores_all_dispatch_globals(invocation):
    before = globals_snapshot()

    def failed(*a, **kw):
        raise OSError("CPU stub launch error")

    assert entry.supervise(invocation, popen_factory=failed) == 1
    assert globals_snapshot() == before


def test_native_failure_is_not_hidden_by_warmup_success(invocation, monkeypatch):
    popen, _, _, _, _ = successful_child(invocation, monkeypatch, native_exit=3221225477)
    assert entry.supervise(invocation, popen_factory=popen) == 1
    result = json.loads((invocation.supervisor_dir / "completion.json").read_bytes())
    assert result["child_returncode_hex32"] == "0xc0000005"


def test_constructor_observability_is_exact_passthrough_and_preserves_exception(
    invocation, monkeypatch, capsys
):
    manifest, _, _ = fake_modeling_utils(invocation, monkeypatch)
    trainer = sys.modules["scripts.train_onboard_vlm_navigation_pilot"]
    original = trainer.NavigationBackend.__init__
    calls = []
    trainer.warmup = lambda *a: calls.append(a)
    state, restore = entry.install_constructor_traceback(manifest)
    try:
        backend = trainer.NavigationBackend("original snapshot", "original budget")
        assert backend.snapshot == "original snapshot" and backend.budget == "original budget"
        assert len(calls) == 1
        with pytest.raises(RuntimeError) as exc:
            trainer.NavigationBackend("raise", "unchanged budget")
        assert exc.value is trainer.ORIGINAL_EXCEPTION
        assert "original constructor failure" in capsys.readouterr().err
    finally:
        restore()
    assert trainer.NavigationBackend.__init__ is original
    assert state["calls_attempted"] == 2 and state["exceptions_observed"] == 1
    assert state["restored"] is True


def test_restoration_error_cannot_write_success_receipt(invocation, monkeypatch):
    fake_modeling_utils(invocation, monkeypatch)
    original_install = entry.install_warmup_skip

    def failed_restore(manifest):
        state, _ = original_install(manifest)

        def restore():
            raise RuntimeError("fixture restoration failed")

        return state, restore

    monkeypatch.setattr(entry, "install_warmup_skip", failed_restore)
    monkeypatch.setattr(streaming, "install_streaming_loader", lambda *a: {})
    before = globals_snapshot()

    def stub(args):
        args.output_dir.mkdir()
        streaming.install_streaming_loader({}, [])
        sys.modules["scripts.train_onboard_vlm_navigation_pilot"].NavigationBackend(
            "snapshot", "budget"
        )
        return 0

    monkeypatch.setattr(streaming, "worker", stub)
    assert entry.worker(invocation) == 1
    assert globals_snapshot() == before
    report = json.loads((invocation.output_dir / "allocator-warmup-report.json").read_bytes())
    assert report["worker_returncode"] == 1
    assert report["warmup_observation"]["warmup_override_restored"] is False


def test_missing_actual_warmup_calls_fail_worker_without_fake_completion(invocation, monkeypatch):
    fake_modeling_utils(invocation, monkeypatch)
    monkeypatch.setattr(streaming, "install_streaming_loader", lambda *a: {})

    def stub(args):
        args.output_dir.mkdir()
        streaming.install_streaming_loader({}, [])
        return 0

    monkeypatch.setattr(streaming, "worker", stub)
    assert entry.worker(invocation) == 1
    report = json.loads((invocation.output_dir / "allocator-warmup-report.json").read_bytes())
    assert report["warmup_observation"]["warmup_calls_skipped"] == 0

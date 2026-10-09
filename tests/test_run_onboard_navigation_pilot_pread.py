"""CPU stubs exercise the declared compatibility path; no native model is loaded."""

from __future__ import annotations

import contextlib
import io
import json
import random
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts import run_onboard_navigation_pilot_pread as entry

sup = entry.sup


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    bound_file = sealed / "original"
    bound_file.write_bytes(b"unchanged frozen source")
    bound = sup.file_record(bound_file)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    plan = {
        "artifact_root": str(tmp_path),
        "dataset_report": bound,
        "dataset_samples": bound,
        "review_receipt": bound,
        "source_files": [bound],
        "base": {"snapshot": str(snapshot), "safetensors": bound, "processor_files": [bound]},
        "expected_samples": 86,
        "train_sample_ids": [f"sample-{i}" for i in range(86)],
        "optimization": {"seed": 20261007},
    }
    plan_path = tmp_path / "plan.json"
    write_json(plan_path, plan)
    new_venv = tmp_path / "new-venv"
    executable = new_venv / "Scripts/python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"bound CPU fixture interpreter identity")
    origin = new_venv / "Lib/site-packages/safetensors/__init__.py"
    origin.parent.mkdir(parents=True)
    origin.write_bytes(b"bound CPU fixture safetensors identity")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    manifest_path = runtime_dir / "manifest.json"
    manifest = {
        "schema_version": 1,
        "contract": entry.CONTRACT,
        "artifact_root": str(tmp_path),
        "plan_sha256": sup.sha256_file(plan_path),
        "entrypoint_sha256": sup.sha256_file(entry.__file__),
        "python_executable": str(executable),
        "package_versions": entry.FIXED_VERSIONS.copy(),
        "declared_loader_backend": "pread",
        "package_upgrade_only_claim": False,
        "source_files": [sup.file_record(origin), sup.file_record(executable)],
    }
    write_json(manifest_path, manifest)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(entry.importlib.metadata, "version", entry.FIXED_VERSIONS.__getitem__)
    monkeypatch.setattr(
        entry.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=str(origin))
    )
    return SimpleNamespace(
        plan=plan_path,
        plan_sha256=sup.sha256_file(plan_path),
        output_dir=tmp_path / "worker",
        supervisor_dir=tmp_path / "supervisor",
        mode="load-only",
        runtime_manifest=manifest_path,
        runtime_manifest_sha256=sup.sha256_file(manifest_path),
        worker=False,
    )


def mutate_manifest(args, function):
    value = json.loads(args.runtime_manifest.read_bytes())
    function(value)
    write_json(args.runtime_manifest, value)
    args.runtime_manifest_sha256 = sup.sha256_file(args.runtime_manifest)


def fake_safetensors(monkeypatch, *, raises=False):
    root = ModuleType("safetensors")
    root.__path__ = []
    root.__version__ = "0.8.0"
    torch = ModuleType("safetensors.torch")
    calls = []
    handle = object()

    def original(*args, **kwargs):
        calls.append((args, kwargs.copy()))
        if raises:
            raise OSError("CPU fixture load failed")
        return handle

    original.__text_signature__ = "(filename, framework, device=None, backend='mmap')"
    root.safe_open = original
    torch.safe_open = lambda *args, **kwargs: pytest.fail("unpatched torch alias")
    root.torch = torch
    monkeypatch.setitem(sys.modules, "safetensors", root)
    monkeypatch.setitem(sys.modules, "safetensors.torch", torch)
    return root, torch, calls, handle


def worker_receipt(args, counts, *, pid=12345):
    return {
        "contract": entry.CONTRACT,
        **sup.CLAIMS,
        "mode": args.mode,
        "pid": pid,
        "plan_sha256": args.plan_sha256,
        "runtime_manifest_sha256": args.runtime_manifest_sha256,
        "effective_loader_backend": "pread",
        "package_upgrade_only_claim": False,
        "change_scope": "this_child_only",
        "worker_returncode": 0,
        "observed_calls": counts.copy(),
    }


def successful_child(args, monkeypatch, *, mutate=None, malformed=False, native_exit=0):
    root, torch, calls, handle = fake_safetensors(monkeypatch)
    commands = []

    class Child:
        pid = 98765

        def wait(self):
            return native_exit

    def popen(command, **kwargs):
        commands.append(command)
        assert kwargs["shell"] is False
        assert kwargs["cwd"] == str(sup.WORKSPACE)
        args.output_dir.mkdir()
        report = {
            "contract": sup.LOAD_CONTRACT,
            "native_exit_code": 0,
            "backend_initialized": True,
            "optimizer_performed": False,
            "inference_performed": False,
            "forward_backward_performed": False,
            "plan_sha256": args.plan_sha256,
            **sup.CLAIMS,
        }
        if args.mode == "load-only":
            write_json(args.output_dir / "load-only-report.json", report)
        else:
            report.update(
                contract="multidetect.onboard-vlm-navigation-pilot-training-v1",
                training_plan_sha256=args.plan_sha256,
                **dict.fromkeys(sup.COUNTERS, 86),
            )
            write_json(args.output_dir / "report.json", report)
            plan = json.loads(args.plan.read_bytes())
            samples = list(plan["train_sample_ids"])
            random.Random(plan["optimization"]["seed"]).shuffle(samples)
            rows = [
                json.dumps(
                    {
                        "stage": key.replace("optimizer_steps_", "optimizer_step_"),
                        "step": step,
                        "epoch": 1,
                        "split": "train",
                        "sample_id": sample,
                    }
                )
                for step, sample in enumerate(samples, 1)
                for key in sup.COUNTERS
            ]
            (args.output_dir / "training-events.jsonl").write_text(
                "\n".join(rows) + "\n", encoding="utf-8"
            )
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            sup.emit(
                "pread_runtime_verified",
                pid=12345,
                mode=args.mode,
                plan_sha256=args.plan_sha256,
                runtime_manifest_sha256=args.runtime_manifest_sha256,
            )
            counts = entry.install_pread_loader()
            assert root.safe_open("base", framework="pt") is handle
            assert torch.safe_open("adapter", framework="pt", backend="mmap") is handle
            receipt = worker_receipt(args, counts)
            sup.emit("explicit_pread_worker_finished", **receipt)
        kwargs["stdout"].write(stream.getvalue().encode("utf-8"))
        if mutate is not None:
            mutate(receipt)
        path = args.output_dir / "pread-loader-report.json"
        if malformed:
            path.write_bytes(b'{"malformed":')
        else:
            write_json(path, receipt)
        return Child()

    return popen, commands, calls


def test_runtime_admission_is_stdlib_and_binds_origin(invocation):
    before = set(sys.modules)
    manifest, _, root, records = entry.read_runtime(invocation)
    assert root == invocation.plan.parent
    assert manifest["package_versions"] == entry.FIXED_VERSIONS
    assert sup.file_record(invocation.runtime_manifest) in records
    assert sup.file_record(entry.__file__) in records
    assert not {"torch", "transformers", "peft", "accelerate"} & (set(sys.modules) - before)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("declared_loader_backend", "mmap"),
        ("package_upgrade_only_claim", True),
        ("plan_sha256", "0" * 64),
        ("entrypoint_sha256", "0" * 64),
        ("contract", "wrong"),
        ("python_executable", "relative-python.exe"),
        ("artifact_root", "relative-root"),
    ],
)
def test_runtime_identity_mismatch_rejected_before_outputs(invocation, field, value):
    mutate_manifest(invocation, lambda manifest: manifest.__setitem__(field, value))
    with pytest.raises((ValueError, FileNotFoundError)):
        entry.supervise(invocation)
    assert not invocation.supervisor_dir.exists()
    assert not invocation.output_dir.exists()


@pytest.mark.parametrize("kind", ["version", "extra-version", "source", "duplicate", "origin"])
def test_actual_bound_runtime_mismatch_rejected(invocation, monkeypatch, kind):
    if kind == "version":
        monkeypatch.setattr(entry.importlib.metadata, "version", lambda name: "wrong")
    elif kind == "extra-version":
        mutate_manifest(invocation, lambda m: m["package_versions"].update(extra="1.0"))
    elif kind == "source":
        source = json.loads(invocation.runtime_manifest.read_bytes())["source_files"][0]["path"]
        Path(source).write_bytes(b"mutated runtime source")
    elif kind == "duplicate":
        mutate_manifest(invocation, lambda m: m["source_files"].append(m["source_files"][0]))
    else:
        monkeypatch.setattr(
            entry.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=entry.__file__)
        )
    with pytest.raises(ValueError):
        entry.read_runtime(invocation)


def test_manifest_hash_and_duplicate_json_fail_closed(invocation):
    invocation.runtime_manifest_sha256 = "0" * 64
    with pytest.raises(ValueError, match="SHA"):
        entry.read_runtime(invocation)
    invocation.runtime_manifest.write_bytes(b'{"x":1,"x":2}')
    invocation.runtime_manifest_sha256 = sup.sha256_file(invocation.runtime_manifest)
    with pytest.raises(ValueError, match="Duplicate"):
        entry.read_runtime(invocation)


def test_loader_changes_both_aliases_and_explicit_mmap(monkeypatch, capsys):
    root, torch, calls, handle = fake_safetensors(monkeypatch)
    counts = entry.install_pread_loader()
    assert root.safe_open is torch.safe_open
    assert root.safe_open("base", framework="pt", device="cpu") is handle
    assert torch.safe_open("adapter", framework="pt", backend="mmap") is handle
    assert [call[1]["backend"] for call in calls] == ["pread", "pread"]
    assert counts == {"safe_open_calls_attempted": 2, "safe_open_calls_returned": 2}
    assert '"requested_backend": "mmap"' in capsys.readouterr().out
    with pytest.raises(ValueError, match="keyword backend"):
        root.safe_open("base", "pt", "cpu", "mmap")
    assert len(calls) == 2


def test_failed_open_records_attempt_only(monkeypatch):
    root, _, _, _ = fake_safetensors(monkeypatch, raises=True)
    counts = entry.install_pread_loader()
    with pytest.raises(OSError):
        root.safe_open("base", framework="pt")
    assert counts == {"safe_open_calls_attempted": 1, "safe_open_calls_returned": 0}


def test_supervision_reuses_frozen_parent_and_real_stub_loader_events(invocation, monkeypatch):
    command_before, capture_before = sup.child_command, sup.capture_sources
    popen, commands, calls = successful_child(invocation, monkeypatch)
    assert entry.supervise(invocation, popen_factory=popen) == 0
    assert sup.child_command is command_before and sup.capture_sources is capture_before
    command = commands[0]
    assert command[7] == str(Path(entry.__file__).resolve())
    assert "--worker" in command and "--runtime-manifest-sha256" in command
    assert len(calls) == 2
    value = json.loads((invocation.supervisor_dir / "completion.json").read_bytes())
    result = json.loads((invocation.supervisor_dir / "pread-completion.json").read_bytes())
    assert value["child_completion_verified"] and value["source_bindings_unchanged"]
    assert result["pread_receipt_valid"] and result["supervisor_exit_code"] == 0
    assert result["pread_receipt_binding"] == sup.file_record(
        invocation.output_dir / "pread-loader-report.json"
    )
    assert all(result[name] is False for name in sup.CLAIMS)


def test_train_keeps_original_exact_86_step_journal_gate(invocation, monkeypatch):
    invocation.mode = "train"
    popen, _, _ = successful_child(invocation, monkeypatch)
    assert entry.supervise(invocation, popen_factory=popen) == 0
    value = json.loads((invocation.supervisor_dir / "completion.json").read_bytes())
    assert value["worker_report"]["counters"] == dict.fromkeys(sup.COUNTERS, 86)
    assert value["optimizer_journal"]["observed_counters"] == dict.fromkeys(sup.COUNTERS, 86)
    assert value["child_completion_verified"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_sha256", "0" * 64),
        ("runtime_manifest_sha256", "0" * 64),
        ("mode", "train"),
        ("pid", True),
        ("change_scope", "all-processes"),
        ("worker_returncode", True),
        ("worker_returncode", 1),
        ("candidate_promoted", True),
        ("effective_loader_backend", "mmap"),
        ("observed_calls", {"safe_open_calls_attempted": 1, "safe_open_calls_returned": 1}),
    ],
)
def test_receipt_identity_and_count_mismatch_cannot_claim_success(
    invocation, monkeypatch, field, value
):
    popen, _, _ = successful_child(
        invocation, monkeypatch, mutate=lambda r: r.__setitem__(field, value)
    )
    assert entry.supervise(invocation, popen_factory=popen) == 1
    result = json.loads((invocation.supervisor_dir / "pread-completion.json").read_bytes())
    assert result["sealed_supervisor_exit_code"] == 0
    assert result["supervisor_exit_code"] == 1 and not result["pread_receipt_valid"]


def test_malformed_report_does_not_prevent_parent_completion(invocation, monkeypatch):
    popen, _, _ = successful_child(invocation, monkeypatch, malformed=True)
    assert entry.supervise(invocation, popen_factory=popen) == 1
    result = json.loads((invocation.supervisor_dir / "pread-completion.json").read_bytes())
    assert result["pread_receipt_failure"]["type"] == "JSONDecodeError"


def test_native_failure_still_has_frozen_supervisor_evidence(invocation, monkeypatch):
    popen, _, _ = successful_child(invocation, monkeypatch, native_exit=3221225477)
    assert entry.supervise(invocation, popen_factory=popen) == 1
    result = json.loads((invocation.supervisor_dir / "completion.json").read_bytes())
    assert result["child_returncode_hex32"] == "0xc0000005"
    assert result["source_bindings_unchanged"]


def test_hooks_restore_after_parent_failure(invocation):
    original_command, original_capture = sup.child_command, sup.capture_sources

    def failed_launch(*args, **kwargs):
        raise OSError("fixture launch failure")

    assert entry.supervise(invocation, popen_factory=failed_launch) == 1
    assert sup.child_command is original_command and sup.capture_sources is original_capture


@pytest.mark.parametrize("kind", ["runtime-manifest", "runtime-source", "base", "dataset"])
def test_outputs_cannot_pollute_bound_directories(invocation, kind):
    if kind == "runtime-manifest":
        parent = invocation.runtime_manifest.parent
    elif kind == "runtime-source":
        parent = Path(
            json.loads(invocation.runtime_manifest.read_bytes())["source_files"][0]["path"]
        ).parent
    else:
        parent = invocation.plan.parent / ("snapshot" if kind == "base" else "sealed")
    invocation.output_dir = parent / "new-output"
    with pytest.raises(ValueError):
        entry.supervise(invocation)
    assert not invocation.output_dir.exists()
    assert not invocation.supervisor_dir.exists()


def test_existing_output_is_not_overwritten(invocation):
    invocation.output_dir.mkdir()
    marker = invocation.output_dir / "marker"
    marker.write_bytes(b"user content")
    with pytest.raises(FileExistsError):
        entry.supervise(invocation)
    assert marker.read_bytes() == b"user content"


def test_worker_postverification_failure_cannot_report_zero(invocation, monkeypatch):
    original_read = entry.read_runtime
    read_count = 0

    def changed_after(args):
        nonlocal read_count
        manifest, plan, root, records = original_read(args)
        read_count += 1
        if read_count == 2:
            records = [*records, {"path": "changed", "bytes": 1, "sha256": "0" * 64}]
        return manifest, plan, root, records

    def loaded(args):
        args.output_dir.mkdir()
        return 0

    monkeypatch.setattr(entry, "read_runtime", changed_after)
    monkeypatch.setattr(
        entry,
        "install_pread_loader",
        lambda: {"safe_open_calls_attempted": 1, "safe_open_calls_returned": 1},
    )
    monkeypatch.setattr(sup, "worker_load_only", loaded)
    with pytest.raises(ValueError, match="Runtime changed"):
        entry.worker(invocation)
    receipt = json.loads((invocation.output_dir / "pread-loader-report.json").read_bytes())
    assert receipt["worker_returncode"] == 1


def test_missing_real_loader_events_disqualify_claimed_report(invocation):
    counts = {"safe_open_calls_attempted": 1, "safe_open_calls_returned": 1}
    log = invocation.plan.parent / "stdout.bin"
    log.write_bytes(b"ordinary fixture log with no loader event\n")
    with pytest.raises(ValueError, match="do not prove"):
        entry.verify_loader_events(log, worker_receipt(invocation, counts), invocation)


@pytest.mark.parametrize("kind", ["reorder", "backend", "counter", "identity", "finish"])
def test_loader_event_stream_must_match_actual_report(invocation, monkeypatch, kind):
    popen, _, _ = successful_child(invocation, monkeypatch)
    assert entry.supervise(invocation, popen_factory=popen) == 0
    log = invocation.supervisor_dir / "stdout.bin"
    rows = [json.loads(line) for line in log.read_bytes().splitlines()]
    receipt = json.loads((invocation.output_dir / "pread-loader-report.json").read_bytes())
    if kind == "reorder":
        rows[2], rows[3] = rows[3], rows[2]
    elif kind == "backend":
        rows[2]["effective_backend"] = "mmap"
    elif kind == "counter":
        rows[2]["safe_open_calls_attempted"] = True
    elif kind == "identity":
        rows[0]["runtime_manifest_sha256"] = "0" * 64
    else:
        rows[-1]["worker_returncode"] = 9
    log.write_bytes(b"\n".join(json.dumps(row).encode() for row in rows) + b"\n")
    with pytest.raises(ValueError):
        entry.verify_loader_events(log, receipt, invocation)

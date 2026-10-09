"""CPU-only recovery gates; frozen real-GPU loading stays with the root agent."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import random
import struct
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts import recover_onboard_navigation_adapter as entry

sup, body = entry.sup, entry.body
spec = importlib.util.spec_from_file_location(
    "recovery_path_cpu_fixture",
    Path(__file__).with_name("test_run_onboard_navigation_pilot_streaming_no_warmup_importpath.py"),
)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    old = base.invocation.__wrapped__(tmp_path, monkeypatch)
    plan = json.loads(old.plan.read_bytes())
    classes = ["forward", "left", "right", "stop"]
    plan["diagnostic_probes"] = [
        {"sample_id": sid, "action_class": cls}
        for sid, cls in zip(plan["train_sample_ids"], classes, strict=False)
    ]
    plan["base"].update(model_id="Qwen/Qwen3-VL-2B-Instruct", revision="pinned CPU fixture")
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    old.plan = config_dir / "plan.json"
    write(old.plan, plan)
    old.plan_sha256 = sup.sha256_file(old.plan)
    old_runtime = json.loads(old.runtime_manifest.read_bytes())
    old_runtime["plan_sha256"] = old.plan_sha256
    write(old.runtime_manifest, old_runtime)
    old.runtime_manifest_sha256 = sup.sha256_file(old.runtime_manifest)
    trained = old.output_dir
    trained.mkdir()
    adapter = trained / "adapter"
    adapter.mkdir()
    weights = adapter / "adapter_model.safetensors"
    header = json.dumps({"x": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    weights.write_bytes(struct.pack("<Q", len(header)) + header + b"\x00\x00\x80\x3f")
    write(adapter / "adapter_config.json", {"CPU_fixture": True})
    probes = [
        {
            **p,
            "parent": False,
            "actual_prefix_tensors": {},
            "generated": {"raw_output": "bound CPU fixture", "output_token_ids": [1, 2]},
            "diagnostic": {
                "strict_json_valid": False,
                "quality_claim": False,
                "resubstitution_only": True,
            },
        }
        for p in plan["diagnostic_probes"]
    ]
    prepared = {sid: {"prefix_tensors": {}} for sid in plan["train_sample_ids"]}
    write(trained / "candidate-probes.json", probes)
    write(trained / "parent-probes.json", [{**p, "parent": True} for p in probes])
    write(trained / "prepared-inputs.json", prepared)
    samples = list(plan["train_sample_ids"])
    random.Random(plan["optimization"]["seed"]).shuffle(samples)
    rows = [
        {
            "stage": key.replace("optimizer_steps_", "optimizer_step_"),
            "step": step,
            "sample_id": sid,
            "epoch": 1,
            "split": "train",
        }
        for step, sid in enumerate(samples, 1)
        for key in sup.COUNTERS
    ]
    (trained / "training-events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    before = sup.capture_sources(plan, tmp_path, old.plan)
    old.supervisor_dir.mkdir()
    write(old.supervisor_dir / "preflight.json", {"sources_before": before})
    write(
        old.supervisor_dir / "completion.json",
        {
            "sources_before": before,
            "sources_after": before,
            "source_bindings_unchanged": True,
            "supervisor_exit_code": 1,
            "child_returncode_raw": 1,
            "child_completion_verified": False,
        },
    )
    adapter_records = [sup.file_record(weights), sup.file_record(adapter / "adapter_config.json")]
    outputs = adapter_records + [
        sup.file_record(trained / name)
        for name in (
            "parent-probes.json",
            "candidate-probes.json",
            "prepared-inputs.json",
            "training-events.jsonl",
        )
    ]
    original = {
        "contract": "multidetect.onboard-vlm-navigation-pilot-training-v1",
        **sup.CLAIMS,
        "native_exit_code": 1,
        "failure_type": "OutOfMemoryError",
        "failure": "CPU fixture known post-training OOM",
        "training_plan_sha256": old.plan_sha256,
        "optimizer_update_uncertain": False,
        "base_parameter_bytes_unchanged": True,
        "base_parameter_sha256_before": "a" * 64,
        "base_parameter_sha256_after": "a" * 64,
        "adapter_parameter_sha256_before": "b" * 64,
        "adapter_parameter_sha256_after": "c" * 64,
        **dict.fromkeys(sup.COUNTERS, 86),
        "runtime_files": [],
        "adapter_files": adapter_records,
        "output_artifact_files": outputs,
        "trainable_audit": {
            "trainable_parameters": [{"name": "CPU fixture adapter"}],
            "trainable_parameter_count": 1,
        },
        "optimization": {"rank": 4},
        "model_id": plan["base"]["model_id"],
        "revision": plan["base"]["revision"],
        "declared_train_sample_ids": plan["train_sample_ids"],
        "resubstitution_only": True,
        "independent_holdout_available": False,
    }
    write(trained / "report.json", original)
    files = {
        "plan": sup.file_record(old.plan),
        "runtime_manifest": sup.file_record(old.runtime_manifest),
        "training_report": sup.file_record(trained / "report.json"),
        "parent_probes": sup.file_record(trained / "parent-probes.json"),
        "candidate_probes": sup.file_record(trained / "candidate-probes.json"),
        "prepared_inputs": sup.file_record(trained / "prepared-inputs.json"),
        "training_events": sup.file_record(trained / "training-events.jsonl"),
        "source_preflight": sup.file_record(old.supervisor_dir / "preflight.json"),
        "source_completion": sup.file_record(old.supervisor_dir / "completion.json"),
    }
    records = {
        r["path"]: r
        for r in [
            *before,
            *outputs,
            *old_runtime["source_files"],
            *files.values(),
            sup.file_record(entry.runtime.__file__),
            sup.file_record(entry.__file__),
            sup.file_record(__file__),
        ]
    }
    directory = tmp_path / "recovery-manifest"
    directory.mkdir()
    manifest_path = directory / "manifest.json"
    write(
        manifest_path,
        {
            "schema_version": 1,
            "contract": entry.CONTRACT,
            "artifact_root": str(tmp_path),
            "entrypoint_sha256": sup.sha256_file(entry.__file__),
            "files": files,
            "source_files": list(records.values()),
        },
    )
    return SimpleNamespace(
        recovery_manifest=manifest_path,
        recovery_manifest_sha256=sup.sha256_file(manifest_path),
        output_dir=tmp_path / "recovered",
        supervisor_dir=tmp_path / "recovery-supervisor",
        old=old,
    )


def update_binding(args, path):
    manifest = json.loads(args.recovery_manifest.read_bytes())
    current = sup.file_record(path)
    for key, row in manifest["files"].items():
        if row["path"] == current["path"]:
            manifest["files"][key] = current
    manifest["source_files"] = [
        current if r["path"] == current["path"] else r for r in manifest["source_files"]
    ]
    write(args.recovery_manifest, manifest)
    args.recovery_manifest_sha256 = sup.sha256_file(args.recovery_manifest)


def result_for(admitted):
    probes = admitted.documents["candidate_probes"]
    return {
        "fresh_base_parameter_sha256": "a" * 64,
        "base_parameter_sha256_after": "a" * 64,
        "base_parameter_bytes_unchanged": True,
        "loaded_adapter_parameter_sha256": "c" * 64,
        "exact_adapter_load": {
            "exact_tensor_equality": True,
            "saved_parameter_sha256": "c" * 64,
            "loaded_parameter_sha256": "c" * 64,
            "dtype_conversion_performed": False,
            "tensor_count": 1,
        },
        "probe_count": 4,
        "probe_generation_token_ids_and_raw_equal": True,
        "source_rgb_and_processor_tensors_equal": True,
        "source_rgb_bytes_reverified": True,
        "resubstitution_only": True,
        "regenerated_probes": [
            {
                "sample_id": p["sample_id"],
                "generated": p["generated"],
                "diagnostic": p["diagnostic"],
                "actual_encoded": admitted.documents["prepared_inputs"][p["sample_id"]],
            }
            for p in probes
        ],
    }


def cpu_check(args, monkeypatch, *, fail=False):
    base.base.fake_modeling_utils(args.old, monkeypatch)
    root, torch, calls, handles = base.base.base.fake_safetensors(monkeypatch)
    old_alias = root.safe_open

    def check(admitted):
        sys.modules["scripts.train_onboard_vlm_navigation_pilot"].NavigationBackend(
            "snapshot", "budget"
        )
        base_path = next(
            iter(entry.streaming.checkpoint_paths(admitted.old_args, admitted.plan, admitted.root))
        )
        with root.safe_open(base_path, framework="pt") as opened:
            assert opened.get_tensors() == {"x": "CPU fixture tensor"}
        assert torch.load_file(admitted.weights["path"]) == {"x": "CPU fixture tensor"}
        if fail:
            raise ValueError("CPU fixture original tensor/input mismatch")
        return result_for(admitted)

    return check, calls, handles, root, old_alias


def test_stdlib_admission_rechecks_original_86_journal_and_sources(invocation):
    modules = set(sys.modules)
    admitted = entry.read_manifest(invocation)
    assert admitted.journal["observed_counters"] == dict.fromkeys(sup.COUNTERS, 86)
    assert admitted.original["native_exit_code"] == 1
    assert not {"torch", "transformers", "peft", "accelerate"} & (set(sys.modules) - modules)


@pytest.mark.parametrize(
    "kind",
    ["entry", "sources", "promotion", "base", "counts", "journal", "prepost", "probe", "prepared"],
)
def test_admission_fail_closed_before_outputs(invocation, kind):
    manifest = json.loads(invocation.recovery_manifest.read_bytes())
    if kind == "entry":
        manifest["entrypoint_sha256"] = "0" * 64
    elif kind == "sources":
        manifest["source_files"] = [
            r for r in manifest["source_files"] if r["path"] != str(Path(entry.__file__).resolve())
        ]
    else:
        selected = {
            "promotion": "training_report",
            "base": "training_report",
            "counts": "training_report",
            "journal": "training_events",
            "prepost": "source_completion",
            "probe": "candidate_probes",
            "prepared": "prepared_inputs",
        }[kind]
        path = Path(manifest["files"][selected]["path"])
        if kind == "journal":
            path.write_bytes(b"not an actual 86-step journal\n")
        else:
            value = json.loads(path.read_bytes())
            if kind == "promotion":
                value["candidate_promoted"] = True
            elif kind == "base":
                value["base_parameter_sha256_after"] = "d" * 64
            elif kind == "counts":
                value["optimizer_steps_completed"] = 85
            elif kind == "prepost":
                value["source_bindings_unchanged"] = False
            elif kind == "probe":
                value[0]["sample_id"] = "different"
            else:
                value.pop(next(iter(value)))
            write(path, value)
        update_binding(invocation, path)
        manifest = json.loads(invocation.recovery_manifest.read_bytes())
    write(invocation.recovery_manifest, manifest)
    invocation.recovery_manifest_sha256 = sup.sha256_file(invocation.recovery_manifest)
    with pytest.raises(ValueError):
        entry.supervise(invocation)
    assert not invocation.output_dir.exists() and not invocation.supervisor_dir.exists()


@pytest.mark.parametrize("kind", ["existing", "trained", "runtime", "sealed"])
def test_recovery_cannot_write_into_existing_or_bound_dirs(invocation, kind):
    if kind == "existing":
        invocation.output_dir.mkdir()
        (invocation.output_dir / "marker").write_bytes(b"user content")
    elif kind == "trained":
        invocation.output_dir = invocation.old.output_dir / "new-output"
    elif kind == "runtime":
        invocation.output_dir = invocation.old.runtime_manifest.parent / "new-output"
    else:
        invocation.output_dir = invocation.old.plan.parent.parent / "sealed/new-output"
    before = set(invocation.old.plan.parent.parent.rglob("*"))
    with pytest.raises((ValueError, FileExistsError)):
        entry.worker(invocation)
    assert set(invocation.old.plan.parent.parent.rglob("*")) == before


def test_worker_no_optimizer_original_failed_report_stays_failed(invocation, monkeypatch):
    check, calls, handles, root, old_alias = cpu_check(invocation, monkeypatch)
    original = invocation.old.output_dir / "report.json"
    before = sup.sha256_file(original)
    assert entry.worker(invocation, check=check) == 0
    assert root.safe_open is old_alias and all(h.closed for h in handles)
    assert len(calls) == 2 and sup.sha256_file(original) == before
    report = json.loads((invocation.output_dir / "report.json").read_bytes())
    assert report["optimizer_steps_performed_by_this_check"] == 0
    assert report["original_training_native_exit_code"] == 1
    assert all(report[key] is False for key in entry.CLAIMS)


def test_worker_failure_preserves_exception_and_restores_hooks(invocation, monkeypatch, capsys):
    check, _, handles, root, old_alias = cpu_check(invocation, monkeypatch, fail=True)
    before = base.globals_snapshot()
    assert entry.worker(invocation, check=check) == 1
    assert root.safe_open is old_alias and all(h.closed for h in handles)
    assert base.globals_snapshot() == before
    assert "original tensor/input mismatch" in capsys.readouterr().err


def test_worker_audit_write_failure_still_restores_aliases_and_hooks(invocation, monkeypatch):
    check, _, handles, root, old_alias = cpu_check(invocation, monkeypatch)
    before = base.globals_snapshot()
    original_write = sup.create_json

    def fail_audit(path, value):
        if path.name == "streaming-loader-report.json":
            raise OSError("CPU fixture final audit write failure")
        return original_write(path, value)

    monkeypatch.setattr(sup, "create_json", fail_audit)
    with pytest.raises(OSError, match="final audit write failure"):
        entry.worker(invocation, check=check)
    assert root.safe_open is old_alias and all(h.closed for h in handles)
    assert base.globals_snapshot() == before


def fake_child(invocation, monkeypatch, *, native_exit=0, mutate=None):
    check, _, _, _, _ = cpu_check(invocation, monkeypatch)

    def popen(command, **kwargs):
        assert command[3] == str(Path(entry.__file__).resolve()) and "--worker" in command
        assert kwargs["shell"] is False
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            assert entry.worker(invocation, check=check) == 0
        if mutate is not None:
            mutate()
        kwargs["stdout"].write(captured.getvalue().encode())
        return SimpleNamespace(pid=os.getpid(), wait=lambda: native_exit)

    return popen


def test_external_parent_requires_native_loader_warmup_and_exact_report(invocation, monkeypatch):
    popen = fake_child(invocation, monkeypatch)
    assert entry.supervise(invocation, popen_factory=popen) == 0
    completion = json.loads((invocation.supervisor_dir / "completion.json").read_bytes())
    assert completion["sources_unchanged"] and completion["worker_result_verified"]
    assert completion["child_returncode_raw"] == 0


@pytest.mark.parametrize(
    "kind", ["tokens", "encoded", "digest", "authority", "missing-events", "pid"]
)
def test_checkbox_report_cannot_mask_actual_mismatch(invocation, monkeypatch, kind):
    def mutate():
        path = invocation.output_dir / "report.json"
        report = json.loads(path.read_bytes())
        if kind == "tokens":
            report["reload_check"]["regenerated_probes"][0]["generated"]["output_token_ids"] = [999]
        elif kind == "encoded":
            report["reload_check"]["regenerated_probes"][0]["actual_encoded"] = {"different": True}
        elif kind == "digest":
            report["reload_check"]["loaded_adapter_parameter_sha256"] = "d" * 64
        elif kind == "authority":
            report["execution_authority"] = True
        elif kind == "pid":
            loader_path = invocation.output_dir / "streaming-loader-report.json"
            value = json.loads(loader_path.read_bytes())
            value["pid"] += 1
            write(loader_path, value)
        else:
            (invocation.output_dir / "allocator-warmup-report.json").unlink()
        write(path, report)

    assert (
        entry.supervise(
            invocation, popen_factory=fake_child(invocation, monkeypatch, mutate=mutate)
        )
        == 1
    )


def test_native_crash_is_never_hidden_by_success_report(invocation, monkeypatch):
    assert (
        entry.supervise(
            invocation, popen_factory=fake_child(invocation, monkeypatch, native_exit=3221225477)
        )
        == 1
    )
    completion = json.loads((invocation.supervisor_dir / "completion.json").read_bytes())
    assert completion["child_returncode_raw"] == 3221225477


def direct_reload_fixture(invocation, monkeypatch, *, mismatch=None):
    """Exercise the real reload control flow without importing any GPU package."""
    admitted = entry.read_manifest(invocation)
    calls = []
    budget = SimpleNamespace(rank=4)
    samples = [SimpleNamespace(sample_id=sid) for sid in admitted.plan["train_sample_ids"]]
    probes = {p["sample_id"]: p for p in admitted.documents["candidate_probes"]}
    trainer = ModuleType("scripts.train_onboard_vlm_navigation_pilot")
    trainer.load_plan = lambda *_args: admitted.plan
    trainer.validate_plan_shape = lambda _plan: budget
    trainer.asdict = lambda _budget: {"rank": 5 if mismatch == "budget" else 4}
    trainer.verify_plan_sources = lambda _plan: calls.append("verify_sources")
    trainer.admit_dataset = lambda _plan: (samples, probes)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Recovery must never call training, backward, or an optimizer")

    trainer.run = trainer.train_epoch = trainer.backward = trainer.optimizer = forbidden

    class Session:
        verified = mismatch != "source"

        def __init__(self, root):
            assert root == admitted.root

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def prime(self, values):
            assert values == samples

        def validate_sample(self, sample):
            calls.append(("validate_rgb", sample.sample_id))

    trainer.navigation = SimpleNamespace(NavigationSourceValidationSession=Session)

    def saved_digest(state, audit, *, rank):
        assert state == {"saved": "CPU fixture FP32 tensors"}
        assert audit == admitted.original["trainable_audit"] and rank == 4
        calls.append("saved_digest")
        return ("d" if mismatch == "saved" else "c") * 64

    def load_exact(model, state, audit, digest, *, rank):
        assert model == "CPU fixture unchanged BF16 base + FP32 adapter"
        assert state == {"saved": "CPU fixture FP32 tensors"}
        assert audit == admitted.original["trainable_audit"] and rank == 4
        assert digest == "c" * 64
        calls.append("load_exact")
        return {"exact_tensor_equality": True}

    trainer.recovery = SimpleNamespace(
        verify_config=lambda *_args: calls.append("verify_config"),
        saved_parameter_digest=saved_digest,
        load_saved_exactly=load_exact,
    )

    class Backend:
        base_digest_initial = ("d" if mismatch == "base" else "a") * 64
        trainable_audit = {} if mismatch == "audit" else admitted.original["trainable_audit"]
        model = "CPU fixture unchanged BF16 base + FP32 adapter"

        def __init__(self, snapshot, actual_budget):
            assert snapshot == Path(admitted.plan["base"]["snapshot"])
            assert actual_budget is budget
            calls.append("fresh_backend")

        def encode(self, sample, root, *, session):
            assert root == admitted.root and isinstance(session, Session)
            calls.append(("encode", sample.sample_id))
            encoded = json.loads(
                json.dumps(admitted.documents["prepared_inputs"][sample.sample_id])
            )
            if mismatch == "prefix":
                encoded["prefix_tensors"] = {"different": True}
            encoded.update(prefix=object(), full=object(), labels=object())
            return encoded

        def generate(self, encoded, *, parent):
            assert parent is False
            calls.append("generate")
            generated = {"raw_output": "bound CPU fixture", "output_token_ids": [1, 2]}
            if mismatch == "tokens":
                generated["output_token_ids"] = [999]
            if mismatch == "raw":
                generated["raw_output"] = "different output"
            return generated

    def parameter_digest(model, *, trainable):
        assert model == Backend.model
        calls.append(("parameter_digest", trainable))
        correct = "c" if trainable else "a"
        wrong = mismatch == ("adapter_after" if trainable else "base_after")
        return ("d" if wrong else correct) * 64

    trainer.core = SimpleNamespace(parameter_digest=parameter_digest)
    trainer.check_memory = lambda *_args: calls.append("check_memory")
    trainer.diagnose = lambda _raw, target: (
        {"different": True} if mismatch == "diagnostic" else target["diagnostic"]
    )
    safetensors = ModuleType("safetensors")
    tensor_loader = ModuleType("safetensors.torch")

    def load_file(path, *, device):
        assert path == admitted.weights["path"] and device == "cpu"
        calls.append("load_saved_file_cpu")
        return {"saved": "CPU fixture FP32 tensors"}

    tensor_loader.load_file = load_file
    safetensors.torch = tensor_loader
    monkeypatch.setitem(sys.modules, "scripts.train_onboard_vlm_navigation_pilot", trainer)
    monkeypatch.setattr(
        sys.modules["scripts"], "train_onboard_vlm_navigation_pilot", trainer, raising=False
    )
    monkeypatch.setitem(sys.modules, "safetensors", safetensors)
    monkeypatch.setitem(sys.modules, "safetensors.torch", tensor_loader)
    return admitted, Backend, calls


def test_direct_reload_checks_saved_parameters_four_exact_probes_and_all_rgb(
    invocation, monkeypatch
):
    admitted, backend, calls = direct_reload_fixture(invocation, monkeypatch)
    result = entry.reload_and_probe(admitted, backend_factory=backend)
    assert result["probe_count"] == 4 and result["resubstitution_only"]
    assert calls.count("fresh_backend") == 1 and calls.count("generate") == 4
    assert sum(isinstance(c, tuple) and c[0] == "encode" for c in calls) == 4
    assert sum(isinstance(c, tuple) and c[0] == "validate_rgb" for c in calls) == 86
    assert calls.count("verify_sources") == 2
    assert calls[-3:] == [
        ("parameter_digest", False),
        ("parameter_digest", True),
        "verify_sources",
    ]
    assert result["base_parameter_sha256_after"] == "a" * 64
    assert result["loaded_adapter_parameter_sha256"] == "c" * 64


@pytest.mark.parametrize(
    "mismatch",
    [
        "budget",
        "saved",
        "base",
        "audit",
        "prefix",
        "tokens",
        "raw",
        "diagnostic",
        "source",
        "base_after",
        "adapter_after",
    ],
)
def test_direct_reload_rejects_actual_parameter_input_output_drift(
    invocation, monkeypatch, mismatch
):
    admitted, backend, _calls = direct_reload_fixture(invocation, monkeypatch, mismatch=mismatch)
    with pytest.raises(ValueError):
        entry.reload_and_probe(admitted, backend_factory=backend)

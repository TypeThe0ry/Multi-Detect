"""CPU stand-in orchestration, not native official processor or model evidence."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import sys
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import onboard_navigation_closed_sample_encoding as closed
from scripts import onboard_navigation_host_memory as memory
from scripts import profile_onboard_navigation_closed_samples as profile
from scripts import train_onboard_navigation_streamed as legacy
from scripts import verify_onboard_navigation_actor_inputs as checker
from scripts import verify_onboard_navigation_closed_cpu_inputs as cpu


class Lease:
    pass


def metadata():
    record = {"shape": [1, 3], "dtype": "torch.int64", "sha256": "a" * 64}
    return {
        "prefix_tokens": 3,
        "total_tokens": 11,
        "response_tokens": 8,
        "supervised_tokens": 8,
        "prefix_tensors": {name: copy.deepcopy(record) for name in checker.FIELDS},
        "input_tensors": {name: copy.deepcopy(record) for name in checker.FIELDS},
        "response_labels_sha256": "b" * 64,
        "target_sha256": "c" * 64,
        "actor_identity_sha256": "d" * 64,
        "prefix_reference_verified": True,
        "prompt_label_mask_verified": True,
    }


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    monkeypatch.setattr(cpu, "ROOT", tmp_path)
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    ids = [f"sample-{index:03d}" for index in range(86)]
    samples = tuple(SimpleNamespace(sample_id=value) for value in ids)
    expected = {value: metadata() for value in ids}
    refs = {value: {"sample_id": value} for value in ids}
    source = tmp_path / "source.py"
    source.write_bytes(b"bound fixture source")
    before = [legacy.record(source)]
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(b'{"fixture_only": true}\n')
    plan_binding = legacy.record(plan_path)
    args = SimpleNamespace(
        plan=plan_path,
        plan_sha256=plan_binding["sha256"],
        output_dir=tmp_path / "artifacts/diagnostics/cpu86",
    )
    old_plan = {"diagnostics": {"sample_ids": ids}, "cpu_prefix_report": "prefix"}
    plan = {
        "sample_ids": ids,
        "prepared_reference": "prepared",
        "semantic_admission_scope": "inherited_verified_old_all86_source_session_once",
        "encoding_residency": "one_closed_verified_sample_without_tensor_cache",
    }
    budget, processor = object(), object()
    state = SimpleNamespace(calls=[], leases=[], fault=None, gates=[], cuda=False)
    torch = SimpleNamespace(cuda=SimpleNamespace(is_initialized=lambda: state.cuda))
    monkeypatch.setattr(
        cpu, "admit_plan", lambda path, digest: (plan, {}, old_plan, budget, before)
    )
    monkeypatch.setattr(cpu, "admitted_dependencies", lambda: (profile, legacy))
    monkeypatch.setattr(
        legacy,
        "read_bound",
        lambda row: expected if row == "prepared" else {"samples": list(refs.values())},
    )
    monkeypatch.setattr(
        legacy,
        "semantic_admission",
        lambda plan: (
            state.gates.append("semantic") or {"base": {"snapshot": str(tmp_path)}},
            samples,
            {},
            {},
            None,
        ),
    )

    def validate(table, plan, actual_budget):
        state.gates.append("prepared")
        assert actual_budget is budget and set(table) == set(ids) and len(table) == 86
        return table

    monkeypatch.setattr(legacy, "validate_prepared_inputs", validate)
    monkeypatch.setattr(profile, "import_cpu_torch", lambda: torch)
    monkeypatch.setattr(
        checker,
        "official_processor",
        lambda snapshot, actual_budget: state.gates.append("processor") or processor,
    )
    monkeypatch.setattr(
        memory, "snapshot", lambda: {"fixture_only": True, "process_working_set_bytes": 321}
    )

    def encode(sample, root, **kwargs):
        assert all(reference() is None for reference in state.leases)
        assert (
            root == tmp_path
            and kwargs["processor"] is processor
            and kwargs["torch_module"] is torch
        )
        assert kwargs["budget"] is budget and kwargs["prefix_reference"] == refs[sample.sample_id]
        assert kwargs["expected_metadata"] == expected[sample.sample_id]
        session = kwargs["session_factory"](root)
        assert isinstance(session, closed.ClosedNavigationSourceValidationSession)
        session.verified = state.fault != "unverified"
        session._active = state.fault == "active"
        state.calls.append(sample.sample_id)
        if state.fault == "MemoryError" and len(state.calls) == 14:
            raise MemoryError("injected CPU allocation failure")
        value = copy.deepcopy(expected[sample.sample_id])
        value.update({name: Lease() for name in ("prefix", "full", "labels")})
        state.leases.extend(weakref.ref(value[name]) for name in ("prefix", "full", "labels"))
        if state.fault == "metadata":
            value["target_sha256"] = "e" * 64
        elif state.fault == "nan":
            value["total_tokens"] = float("nan")
        elif state.fault == "CUDA":
            state.cuda = True
        return value

    monkeypatch.setattr(closed, "encode_closed_navigation_sample", encode)
    return SimpleNamespace(
        args=args,
        ids=ids,
        samples=samples,
        expected=expected,
        references=refs,
        state=state,
        plan=plan,
        old_plan=old_plan,
        source=source,
        before=before,
        processor=processor,
        torch=torch,
        budget=budget,
    )


def report_for(fixture):
    return json.loads((fixture.args.output_dir / "report.json").read_bytes())


def test_all86_exact_metadata_closed_before_counting_and_no_tensor_cache(fixture):
    assert cpu.run(fixture.args) == 0
    report = report_for(fixture)
    table = json.loads((fixture.args.output_dir / "prepared-inputs.json").read_bytes())
    assert report["success"] is True and report["sample_count"] == 86
    assert report["admitted_sample_ids"] == fixture.ids == fixture.state.calls
    assert report["source_validation_sessions_verified"] == 86
    assert report["all86_metadata_equal"] is True and table == fixture.expected
    assert report["source_bindings_before"] == report["source_bindings_after"] == fixture.before
    assert report["plan"] == legacy.record(fixture.args.plan)
    assert fixture.state.gates == ["semantic", "prepared", "processor", "prepared"]
    assert all(report[name] is False for name in cpu.FALSE_CLAIMS)
    assert all(reference() is None for reference in fixture.state.leases)
    assert (
        cpu.validate_success_report(
            report,
            table,
            fixture.old_plan,
            expected_metadata=fixture.expected,
            expected_plan_sha256=fixture.args.plan_sha256,
        )
        is report
    )


@pytest.mark.parametrize("fault", ["unverified", "active", "metadata", "nan", "CUDA"])
def test_unverified_or_malformed_current_result_never_admitted(fixture, fault):
    fixture.state.fault = fault
    assert cpu.run(fixture.args) == 1
    report = report_for(fixture)
    assert report["sample_count"] == report["source_validation_sessions_verified"] == 0
    assert report["admitted_sample_ids"] == [] and not report["success"]
    assert report["failure"]["type"] == "ValueError"
    assert not (fixture.args.output_dir / "prepared-inputs.json").exists()
    assert fixture.state.calls == [fixture.ids[0]]


def test_MemoryError_keeps_exact13_admitted_rows_without_retry_or_complete_artifact(fixture):
    fixture.state.fault = "MemoryError"
    assert cpu.run(fixture.args) == 1
    report = report_for(fixture)
    assert report["failure"]["type"] == "MemoryError"
    assert report["sample_count"] == report["source_validation_sessions_verified"] == 13
    assert report["admitted_sample_ids"] == fixture.ids[:13]
    assert fixture.state.calls == fixture.ids[:14]
    assert not report["all86_metadata_equal"] and not report["source_bindings_unchanged"]
    assert not (fixture.args.output_dir / "prepared-inputs.json").exists()


def test_wrong_semantic86_coverage_fails_before_processor_or_encoding(fixture, monkeypatch):
    monkeypatch.setattr(
        legacy, "semantic_admission", lambda plan: ({}, fixture.samples[:-1], {}, {}, None)
    )
    assert cpu.run(fixture.args) == 1
    assert fixture.state.calls == [] and "processor" not in fixture.state.gates
    assert "all86" in report_for(fixture)["failure"]["message"]


def test_current_source_drift_blocks_complete_artifact_even_after86_rows(fixture, monkeypatch):
    original = legacy.record

    def record(path):
        row = original(path)
        if Path(path) == fixture.source and len(fixture.state.calls) == 86:
            row["sha256"] = "f" * 64
        return row

    monkeypatch.setattr(legacy, "record", record)
    assert cpu.run(fixture.args) == 1
    report = report_for(fixture)
    assert report["sample_count"] == 86 and report["all86_metadata_equal"]
    assert not report["source_bindings_unchanged"] and not report["success"]
    assert not (fixture.args.output_dir / "prepared-inputs.json").exists()


def test_host_snapshot_failure_is_native_failure_not_synthetic_zero(fixture, monkeypatch):
    def fail():
        raise OSError("host memory API failure")

    monkeypatch.setattr(memory, "snapshot", fail)
    assert cpu.run(fixture.args) == 1
    report = report_for(fixture)
    assert report["observations"] == [] and report["failure"]["type"] == "OSError"
    assert fixture.state.calls == []


def test_existing_output_remains_unchanged(fixture):
    fixture.args.output_dir.mkdir()
    marker = fixture.args.output_dir / "report.json"
    marker.write_bytes(b"old receipt")
    with pytest.raises(FileExistsError):
        cpu.run(fixture.args)
    assert marker.read_bytes() == b"old receipt" and fixture.state.calls == []


@pytest.mark.parametrize(
    "field",
    [
        "success",
        "official_processor_used",
        "all86_metadata_equal",
        "source_bindings_unchanged",
        "CUDA_uninitialized_before",
        "CUDA_uninitialized_after",
        "sample_count",
        "source_validation_sessions_verified",
        "sample_ids",
        "admitted_sample_ids",
        "source_bindings_after",
        "quality_claim",
        "plan_sha256",
        "prepared_inputs",
    ],
)
def test_consumer_gate_rejects_incomplete_or_wrong_bindings(fixture, field):
    assert cpu.run(fixture.args) == 0
    report = report_for(fixture)
    if field == "prepared_inputs":
        report[field]["sha256"] = "f" * 64
    elif field == "source_bindings_after":
        report[field] = []
    elif field in {"sample_ids", "admitted_sample_ids"}:
        report[field].reverse()
    elif field in {"sample_count", "source_validation_sessions_verified"}:
        report[field] = True
    elif field == "plan_sha256":
        report[field] = "f" * 64
    else:
        report[field] = not report[field]
    with pytest.raises(ValueError):
        cpu.validate_success_report(
            report,
            fixture.expected,
            fixture.old_plan,
            expected_metadata=fixture.expected,
            expected_plan_sha256=fixture.args.plan_sha256,
        )


def test_three_argument_consumer_still_reads_SHA_bound_original86_reference(fixture, monkeypatch):
    assert cpu.run(fixture.args) == 0
    path = Path(fixture.args.plan).parent / "old-prepared.json"
    path.write_bytes(cpu.canonical(fixture.expected) + b"\n")
    monkeypatch.setattr(cpu, "PREPARED_PATH", str(path))
    monkeypatch.setattr(cpu, "PREPARED_SHA", hashlib.sha256(path.read_bytes()).hexdigest())
    assert cpu.validate_success_report(report_for(fixture), fixture.expected, fixture.old_plan)[
        "success"
    ]
    altered = copy.deepcopy(fixture.expected)
    altered[fixture.ids[0]]["actor_identity_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        cpu.validate_success_report(report_for(fixture), altered, fixture.old_plan)


def test_NEW_source_drift_fails_before_dependency_import(tmp_path, monkeypatch):
    monkeypatch.setattr(cpu, "ROOT", tmp_path)
    rows = []
    for name in cpu.NEW_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original source")
        rows.append(legacy.record(path))
    path = tmp_path / "plan.json"
    path.write_bytes(cpu.canonical({"source_files": rows}))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / cpu.NEW_FILES[0]).write_bytes(b"changed source")
    imports = []

    def forbidden():
        imports.append(True)
        raise AssertionError("Dependency imported before NEW source SHA")

    monkeypatch.setattr(cpu, "admitted_dependencies", forbidden)
    with pytest.raises(ValueError, match="SHA changed"):
        cpu.admit_plan(path, digest)
    assert imports == []


def test_emit_plan_preserves_raw_Python_float_zero(monkeypatch):
    stream = io.BytesIO()
    monkeypatch.setattr(cpu, "build_plan", lambda: {"yaw": 0.0})
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=stream))
    assert cpu.main(["--emit-plan"]) == 0
    assert stream.getvalue() == b'{"yaw": 0.0}\n'


@pytest.mark.parametrize(
    "args", [[], ["--emit-plan", "--plan", "x"], ["--plan", "x", "--plan-sha256", "a" * 64]]
)
def test_CLI_requires_one_complete_mode(args):
    with pytest.raises(SystemExit) as error:
        cpu.main(args)
    assert error.value.code == 2


def test_static_import_loads_no_heavy_dependencies():
    path = Path(cpu.__file__).resolve()
    code = (
        "import importlib.util,sys; "
        f"spec=importlib.util.spec_from_file_location('closed_cpu',{str(path)!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert not {'torch','transformers','PIL','peft','datasets','accelerate'} "
        "& set(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr

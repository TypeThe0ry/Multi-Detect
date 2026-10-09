"""NEW v2 small software fixtures, not the actual122 processor/ML execution."""

import hashlib
import importlib
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import audit_onboard_navigation_dataset_overlap as controls
from scripts import verify_onboard_navigation_indexed_cpu_inputs_v2 as audit


def fixture_helpers():
    path = Path(__file__).with_name("test_verify_onboard_navigation_indexed_cpu_inputs.py")
    spec = importlib.util.spec_from_file_location("_sealed_cpu122_test_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


old = fixture_helpers()
H = "a" * 64


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit.base, "ROOT", tmp_path)
    monkeypatch.setattr(controls, "ROOT", tmp_path)
    (tmp_path / "artifacts/diagnostics").mkdir(parents=True)
    (tmp_path / "configs/training").mkdir(parents=True)
    return tmp_path


def test_scope_blocks_optional_arrow_only_and_preserves_legal_dependency_finder(monkeypatch):
    original = list(sys.meta_path)
    monkeypatch.setattr(sys, "meta_path", original[:])
    state, extra = {}, object()
    with audit.OptionalArrowIsolation(state) as finder:
        assert sys.meta_path[0] is finder
        with pytest.raises(ImportError, match="isolated"):
            importlib.import_module("pyarrow")
        with pytest.raises(ImportError):
            finder.find_spec("pyarrow.dataset")
        assert finder.find_spec("datasets") is None
        assert finder.find_spec("numpy") is None
        sys.meta_path.append(extra)
    assert all(left is right for left, right in zip(sys.meta_path[:-1], original, strict=True))
    assert sys.meta_path[-1] is extra
    assert state["blocked_imports"] == ["pyarrow", "pyarrow.dataset"]
    assert state["own_finder_removed"] and state["prior_finders_preserved"]
    assert len(state["dependency_finders_added"]) == 1
    assert state["pyarrow_modules_after"] == state["datasets_modules_after"] == []


@pytest.mark.parametrize("exception", [ValueError, MemoryError, KeyboardInterrupt, SystemExit])
def test_exception_cleanup_preserves_body_error_and_removes_only_owned_finder(
    monkeypatch, exception
):
    original = list(sys.meta_path)
    monkeypatch.setattr(sys, "meta_path", original[:])
    state, extra = {}, object()
    error = exception("fixture body failure")
    with pytest.raises(exception) as captured:
        with audit.OptionalArrowIsolation(state):
            sys.meta_path.append(extra)
            raise error
    assert captured.value is error
    assert state["own_finder_removed"] and state["prior_finders_preserved"]
    assert sys.meta_path[-1] is extra


@pytest.mark.parametrize("name", ["pyarrow", "pyarrow.lib", "datasets", "datasets.arrow_dataset"])
def test_preloaded_forbidden_modules_rejected_without_module_or_finder_deletion(monkeypatch, name):
    sentinel = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, sentinel)
    before = tuple(sys.meta_path)
    state = {}
    with pytest.raises(ValueError, match="preloaded"):
        with audit.OptionalArrowIsolation(state):
            pytest.fail("Preloaded module must not reach body")
    assert sys.modules[name] is sentinel
    assert tuple(sys.meta_path) == before
    assert state["installed"] is False


@pytest.mark.parametrize("mutation", ["remove", "duplicate", "reorder", "forbidden"])
def test_scope_integrity_failure_cannot_be_hidden_by_finder_equality(monkeypatch, mutation):
    class EqualFinder:
        def __eq__(self, other):
            return True

    prior = [EqualFinder(), EqualFinder()]
    monkeypatch.setattr(sys, "meta_path", prior[:])
    state = {}
    with pytest.raises(ValueError, match="scope cleanup"):
        with audit.OptionalArrowIsolation(state) as finder:
            if mutation == "remove":
                sys.meta_path[:] = [value for value in sys.meta_path if value is not finder]
            elif mutation == "duplicate":
                sys.meta_path.append(finder)
            elif mutation == "reorder":
                sys.meta_path[1:] = prior[::-1]
            else:
                monkeypatch.setitem(sys.modules, "pyarrow", types.ModuleType("pyarrow"))
    assert not any(isinstance(value, audit.OptionalArrowIsolation) for value in sys.meta_path)
    if mutation == "reorder":
        assert state["prior_finders_preserved"] is False


def test_cleanup_error_does_not_replace_existing_body_error(monkeypatch):
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    state, error = {}, ValueError("original failure")
    with pytest.raises(ValueError) as captured:
        with audit.OptionalArrowIsolation(state) as finder:
            sys.meta_path.append(finder)
            raise error
    assert captured.value is error
    assert any("cleanup" in note for note in error.__notes__)
    assert state["own_finder_removed"]


def save_json(sandbox, relative, value):
    target = sandbox / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return controls.bind_file(str(target))


def prior_fixture(sandbox, monkeypatch, mutation=None):
    rows = old.all_rows()
    ids = [row["sample_id"] for row in rows]
    source_report = {"sample_actor_target_bindings": [old.source_actor(row) for row in rows]}
    dataset = sandbox / "dataset.jsonl"
    dataset.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    source_report_binding = save_json(sandbox, "source-report.json", source_report)
    originals = {}
    for name in audit.R1_PINS:
        target = sandbox / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture frozen source\n")
        originals[name] = (target.stat().st_size, hashlib.sha256(target.read_bytes()).hexdigest())
    monkeypatch.setattr(audit, "R1_PINS", originals)
    sources = [controls.bind_file(str(dataset)), source_report_binding]
    plan = {
        "contract": audit.base.CONTRACT + "-plan",
        "output_dir": "artifacts/diagnostics/onboard-navigation-indexed-cpu122-r1",
        "dataset": sources[0],
        "source_report": source_report_binding,
        "sample_ids": ids,
        "sources": sources,
        "runtime": {"source_files": []},
        "processor_files": [],
        "images": [],
        "processor_snapshot": "fixture-local-only",
        "reference_kind": "first-reference-fixture",
    }
    plan_binding = save_json(sandbox, audit.PRIOR_PATHS["plan"], plan)
    references, prepared = [], {}
    for row in rows:
        reference, full = old.metadata(row)
        references.append(reference)
        prepared[row["sample_id"]] = full
    ref_binding = save_json(
        sandbox,
        audit.PRIOR_PATHS["prefix_reference"],
        {
            "reference_kind": plan["reference_kind"],
            "samples": references,
        },
    )
    prepared_binding = save_json(sandbox, audit.PRIOR_PATHS["prepared_inputs"], prepared)
    report = {
        "contract": audit.base.CONTRACT,
        "plan": plan_binding,
        "native_exit_code": 1,
        "success": False,
        "training_dataset_cpu_admitted": False,
        "sample_count": 122,
        "source_validation_sessions_verified": 122,
        "admitted_sample_ids": ids,
        "source_bindings_unchanged": True,
        "source_bindings_before": sources,
        "source_bindings_after": sources,
        "CUDA_uninitialized_before": True,
        "CUDA_uninitialized_after": True,
        "official_processor_used": True,
        "prefix_reference": ref_binding,
        "prepared_inputs": prepared_binding,
        "partial_failures": [
            {
                "type": "ValueError",
                "message": "Forbidden module imported in this phase",
                "admitted_count": 122,
            }
        ],
        **dict.fromkeys(audit.base.FALSE_CLAIMS, False),
    }
    if mutation == "report_success":
        report["success"] = True
    elif mutation == "report_admitted":
        report["training_dataset_cpu_admitted"] = True
    elif mutation == "wrong_error":
        report["partial_failures"][0]["message"] = "different failure"
    elif mutation == "partial":
        report["sample_count"] = 121
    elif mutation == "bool_count":
        report["sample_count"] = True
    elif mutation == "CUDA":
        report["CUDA_uninitialized_after"] = False
    elif mutation == "source_after":
        report["source_bindings_after"] = []
    elif mutation == "reference_binding":
        report["prefix_reference"] = prepared_binding
    report_binding = save_json(sandbox, audit.PRIOR_PATHS["report"], report)
    receipt = {
        "contract": "multidetect.onboard-navigation-indexed-cpu122-failure-evidence-v1",
        "actual_execution": {
            "actual_native_exit_code": 1,
            "processes_terminated": True,
            "optimizer_attempts": 0,
            "model_weights_or_GPU_used": False,
        },
        "observations": {"training_dataset_cpu_admitted": False},
        "claims": {"quality_claim": False},
        "bindings": [
            plan_binding,
            report_binding,
            ref_binding,
            prepared_binding,
            *(controls.bind_file(name) for name in originals),
        ],
    }
    if mutation == "receipt_success":
        receipt["observations"]["training_dataset_cpu_admitted"] = True
    elif mutation == "receipt_native":
        receipt["actual_execution"]["actual_native_exit_code"] = 0
    elif mutation == "optimizer":
        receipt["actual_execution"]["optimizer_attempts"] = 1
    elif mutation == "live":
        receipt["actual_execution"]["processes_terminated"] = False
    elif mutation == "claim":
        receipt["claims"]["quality_claim"] = True
    monkeypatch.setattr(audit, "FAILURE", save_json(sandbox, "failure.json", receipt))
    return plan, rows


def test_only_sealed_false_all122_failure_can_supply_reference(sandbox, monkeypatch):
    plan, _ = prior_fixture(sandbox, monkeypatch)
    prior, values, declared = audit.read_prior(controls)
    assert values["report"]["success"] is False
    assert len(values["prefix_reference"]["samples"]) == len(values["prepared_inputs"]) == 122
    assert prior["plan"] == controls.bind_file(audit.PRIOR_PATHS["plan"])
    assert len(declared) == 6 and plan["sample_ids"] == values["report"]["admitted_sample_ids"]


@pytest.mark.parametrize(
    "mutation",
    [
        "report_success",
        "report_admitted",
        "wrong_error",
        "partial",
        "bool_count",
        "CUDA",
        "source_after",
        "reference_binding",
        "receipt_success",
        "receipt_native",
        "optimizer",
        "live",
        "claim",
    ],
)
def test_modified_prior_failure_narrative_cannot_admit(sandbox, monkeypatch, mutation):
    prior_fixture(sandbox, monkeypatch, mutation)
    with pytest.raises(ValueError):
        audit.read_prior(controls)


def run_fixture(sandbox, monkeypatch):
    prior_plan, rows = prior_fixture(sandbox, monkeypatch)
    prior, _, declared = audit.read_prior(controls)
    runtime = {"package_versions": {}, "source_files": [], "module_origins": {}}
    plan = {
        **prior_plan,
        "contract": audit.CONTRACT + "-plan",
        "output_dir": "artifacts/diagnostics/onboard-navigation-indexed-cpu122-v2-fixture",
        "prior_failed_attempt": prior,
        "optional_arrow_isolation": audit.ISOLATION_POLICY,
        "optional_import_runtime": runtime,
        "sources": controls.merge_bindings([*prior_plan["sources"], *declared, audit.FAILURE]),
    }
    save_json(sandbox, "runtime.json", {})
    monkeypatch.setattr(audit.base, "RUNTIME", controls.bind_file("runtime.json"))
    monkeypatch.setattr(audit.base, "bootstrap", lambda: controls)
    monkeypatch.setattr(audit, "build_plan", lambda output: plan)
    monkeypatch.setattr(audit.base, "runtime_inventory", lambda *a: plan["runtime"])
    monkeypatch.setattr(audit.base, "validate_runtime_origins", lambda *a: ["fixture-origin"])
    monkeypatch.setattr(audit, "optional_runtime", lambda *a: runtime)
    monkeypatch.setattr(audit, "validate_optional_origins", lambda *a: {})
    api = old.basic_api()

    def official_processor(*args):
        with pytest.raises(ImportError, match="isolated"):
            importlib.import_module("pyarrow")
        return object()

    api.checker = SimpleNamespace(official_processor=official_processor)
    monkeypatch.setattr(audit.base, "dependencies", lambda *a: api)
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(is_initialized=lambda: False)
    fake_torch.set_num_threads = lambda value: None
    fake_torch.get_num_threads = lambda: 1
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    binding = save_json(sandbox, "plan.json", plan)
    return plan, rows, binding


@pytest.mark.parametrize(
    "failure", ["none", "metadata14", "exit14", "source_after", "datasets", "CUDA", "threads"]
)
def test_run_real122_loop_fixture_partial_and_false_final_gates(sandbox, monkeypatch, failure):
    plan, rows, binding = run_fixture(sandbox, monkeypatch)
    calls, rechecks = [], []

    def encode(sample, *args, **kwargs):
        calls.append(sample.sample_id)
        if failure == "exit14" and len(calls) == 14:
            raise ValueError("fixture source closure failed")
        reference, prepared = old.metadata(rows[len(calls) - 1])
        if failure == "metadata14" and len(calls) == 14:
            prepared["response_labels_sha256"] = "b" * 64
        if failure == "datasets":
            monkeypatch.setitem(sys.modules, "datasets", types.ModuleType("datasets"))
        if failure == "CUDA":
            sys.modules["torch"].cuda.is_initialized = lambda: True
        return reference, prepared

    def recheck(*args, **kwargs):
        rechecks.append(1)
        if failure == "source_after" and len(rechecks) == 2:
            raise ValueError("fixture source changed")

    monkeypatch.setattr(audit.base, "encode_first_reference", encode)
    monkeypatch.setattr(audit, "recheck", recheck)
    if failure == "threads":
        sys.modules["torch"].get_num_threads = lambda: 2
    report = audit.run("plan.json", binding["sha256"])
    expected = {
        "none": 122,
        "metadata14": 13,
        "exit14": 13,
        "source_after": 122,
        "datasets": 122,
        "CUDA": 0,
        "threads": 0,
    }[failure]
    assert report["sample_count"] == report["source_validation_sessions_verified"] == expected
    assert report["native_exit_code"] == (0 if failure == "none" else 1)
    assert report["success"] is report["training_dataset_cpu_admitted"] is (failure == "none")
    assert report["saved_cpu122_metadata_exact"] is (failure == "none")
    assert all(report[name] is False for name in audit.base.FALSE_CLAIMS)
    assert report["optional_arrow_isolation"]["own_finder_removed"]
    assert report["optional_arrow_isolation"]["prior_finders_preserved"]
    output = sandbox / plan["output_dir"]
    assert {p.name for p in output.iterdir()} == {
        "report.json",
        "prefix-reference.json",
        "prepared-inputs.json",
    }
    assert len(json.loads((output / "prepared-inputs.json").read_bytes())) == expected
    if failure == "none":
        prior = json.loads(controls.read_bound(plan["prior_failed_attempt"]["prepared_inputs"]))
        assert controls.canonical(prior) == controls.canonical(
            json.loads((output / "prepared-inputs.json").read_bytes())
        )
    with pytest.raises(ValueError):
        audit.run("plan.json", binding["sha256"])


@pytest.mark.parametrize("mutation", ["none", "origin", "version"])
def test_actual_optional_imported_python_origins_are_not_metadata_only_claims(
    monkeypatch, mutation
):
    module = types.ModuleType("pandas")
    module.__file__, module.__version__ = (
        str(Path("/fixture/pandas/__init__.py").resolve()),
        "fixed",
    )
    monkeypatch.setitem(sys.modules, "pandas", module)
    runtime = {
        "module_origins": {"pandas": module.__file__},
        "package_versions": {"pandas": "fixed"},
    }
    if mutation == "origin":
        module.__file__ += ".wrong"
    elif mutation == "version":
        module.__version__ = "wrong"
    if mutation == "none":
        assert audit.validate_optional_origins(runtime) == runtime["module_origins"]
    else:
        with pytest.raises(ValueError):
            audit.validate_optional_origins(runtime)


def test_wrong_plan_sha_does_not_import_processor_or_create_output(sandbox, monkeypatch):
    save_json(sandbox, "plan.json", {})
    monkeypatch.setattr(audit.base, "bootstrap", lambda: controls)
    monkeypatch.setattr(audit, "build_plan", lambda *a: pytest.fail("Unexpected plan evaluation"))
    with pytest.raises(ValueError, match="SHA"):
        audit.run("plan.json", H)
    assert not list((sandbox / "artifacts/diagnostics").iterdir())


def test_stdlib_only_import_does_not_load_arrow_processor_or_model():
    path = str(Path(audit.__file__).resolve())
    code = (
        f"import runpy,sys;runpy.run_path({path!r},run_name='not_main');"
        "assert not any(n.split('.')[0] in "
        "('torch','transformers','PIL','numpy','pyarrow','datasets','peft') for n in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr

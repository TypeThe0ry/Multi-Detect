"""Import-order and shared-budget fixtures only; no real model/processor/GPU."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import experiment_onboard_navigation_action_readout_v2 as v2


def load_r1_tests():
    spec = importlib.util.spec_from_file_location(
        "_sealed_r1_cpu_fixtures",
        v2.ROOT / "tests/test_experiment_onboard_navigation_action_readout.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


r1_tests = load_r1_tests()
cpu_only = r1_tests.cpu_only


@pytest.fixture
def orchestration(tmp_path, monkeypatch):
    f = r1_tests.orchestration.__wrapped__(tmp_path, monkeypatch)
    for name in (
        "safe_path",
        "admit_runtime",
        "admit_plan",
        "load_samples",
        "parameter_identity",
        "freeze_backend",
        "closed_prefix",
        "extract_feature",
        "fit_head",
        "compare_generative_reference",
    ):
        monkeypatch.setattr(v2, name, getattr(r1_tests.experiment, name))
    f.plan["prior_failed_attempt"] = {
        "optimizer_steps": dict.fromkeys(("attempted", "returned", "synchronized", "completed"), 0),
        "maximum_family_optimizer_attempts": 800,
        "spent_before": 0,
        "remaining_maximum_attempts": 800,
        "new_budget_granted": False,
    }
    f.events, f.active = [], False
    env = f.api.apis()
    original_load = env.old.load_backend
    original_import = v2.importlib.import_module
    root = SimpleNamespace(safe_open=lambda *a: None)

    @contextmanager
    def scope(*args):
        f.events.append("hooks_installed")
        f.active = True
        with monkeypatch.context() as local:
            local.setitem(sys.modules, "safetensors", root)
            local.setitem(
                sys.modules, "safetensors.torch", SimpleNamespace(safe_open=root.safe_open)
            )
            local.setitem(
                sys.modules,
                "transformers.modeling_utils",
                SimpleNamespace(safe_open=root.safe_open),
            )
            try:
                yield
            finally:
                f.active = False
                f.events.append("hooks_restored")

    def verify(*args):
        f.events.append("runtime_inside" if f.active else "runtime_final")
        assert f.active or "backend" in f.events
        return {"CPU_fixture_only": True}

    def load(*args):
        assert f.active and f.events[-1] == "runtime_inside"
        v2.verify_streaming_alias()
        f.events.append("backend")
        return original_load(*args)

    def imports(name):
        if name == "scripts.onboard_navigation_action_readout":
            assert not f.active and "backend" in f.events
            f.events.append("head_import")
        return original_import(name)

    monkeypatch.setattr(env.old, "loading_scope", scope)
    monkeypatch.setattr(env.old, "load_backend", load)
    monkeypatch.setattr(v2, "verify_runtime_modules", verify)
    monkeypatch.setattr(v2.importlib, "import_module", imports)
    return f


def test_v2_full_orchestration_scope_then_runtime_then_backend_then_head_shared800(orchestration):
    f = orchestration
    report = v2.run(SimpleNamespace(plan="fixture", plan_sha256="a" * 64))
    assert report["native_exit_code"] == 0 and report["success"] is True
    assert f.events == [
        "hooks_installed",
        "runtime_inside",
        "backend",
        "hooks_restored",
        "head_import",
        "runtime_final",
    ]
    assert report["feature_count"] == 145 and report["raw_prediction_count"] == 231
    assert report["optimizer_steps"] == report["combined_optimizer_steps"]
    assert report["combined_optimizer_steps"] == dict.fromkeys(report["optimizer_steps"], 800)
    assert report["prior_failed_attempt"]["new_budget_granted"] is False
    assert report["model_reader_imported_inside_streaming_scope"] is True
    assert f.calls[:86] == ["old_extract"] * 86
    assert f.calls[86:90] == ["fit"] * 4
    assert f.calls[90:149] == ["dev_extract"] * 59
    assert f.calls[149:] == ["score"] * 290
    assert all(value is False for value in report["claims"].values())
    assert not (f.output.parents[2] / "old-run").exists()


@pytest.mark.parametrize("failure", ["module_verification", "alias", "backend", "cleanup"])
def test_loader_failure_restores_scope_without_feature_or_head_spend(
    orchestration, monkeypatch, failure
):
    f = orchestration
    env = f.api.apis()
    if failure == "module_verification":
        monkeypatch.setattr(
            v2,
            "verify_runtime_modules",
            lambda *a: (_ for _ in ()).throw(ValueError("runtime path")),
        )
    elif failure == "alias":
        monkeypatch.setattr(
            v2, "verify_streaming_alias", lambda: (_ for _ in ()).throw(ValueError("cached alias"))
        )
    elif failure == "backend":
        monkeypatch.setattr(
            env.old, "load_backend", lambda *a: (_ for _ in ()).throw(MemoryError("loader"))
        )
    else:
        monkeypatch.setattr(
            env.recovery,
            "check_loader_cleanup",
            lambda *a: (_ for _ in ()).throw(ValueError("cleanup")),
        )
    report = v2.run(SimpleNamespace(plan="fixture", plan_sha256="a" * 64))
    assert report["native_exit_code"] == 1 and not f.active
    assert "hooks_restored" in f.events and "head_import" not in f.events
    assert report["feature_count"] == report["raw_prediction_count"] == 0
    assert report["combined_optimizer_steps"] == dict.fromkeys(report["optimizer_steps"], 0)
    assert "transformers.modeling_utils" not in sys.modules
    if failure == "cleanup":
        assert report["model_loaded"] is True


def test_preimported_model_reader_fails_before_scope_or_backend(orchestration, monkeypatch):
    f = orchestration
    monkeypatch.setitem(
        sys.modules, "transformers.modeling_utils", SimpleNamespace(safe_open=object())
    )
    report = v2.run(SimpleNamespace(plan="fixture", plan_sha256="a" * 64))
    assert report["native_exit_code"] == 1 and "fresh process" in report["error"]["message"]
    assert f.events == [] and report["model_loaded"] is False
    assert report["combined_optimizer_steps"] == dict.fromkeys(report["optimizer_steps"], 0)


def test_head_failure_preserves_attempt_spend_in_common_budget(orchestration, monkeypatch):
    def fail_head(*args, **kwargs):
        kwargs["ledger"]["attempted"] += 1
        raise MemoryError("head step spent, not returned")

    monkeypatch.setattr(v2, "fit_head", fail_head)
    report = v2.run(SimpleNamespace(plan="fixture", plan_sha256="a" * 64))
    assert report["native_exit_code"] == 1
    assert report["optimizer_steps"] == report["combined_optimizer_steps"]
    assert report["combined_optimizer_steps"] == {
        "attempted": 1,
        "returned": 0,
        "synchronized": 0,
        "completed": 0,
    }
    assert report["prior_failed_attempt"]["new_budget_granted"] is False
    assert report["raw_prediction_count"] == 0 and report["head_locked_before_dev59"] is False


def test_stdlib_cached_from_import_alias_reproduces_missed_base_and_corrected_scope():
    code = r"""
import importlib.util, pathlib, sys, types
path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("experiment_v2", path)
v2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v2)
original_reads, observed_reads = [], []
def original(name): original_reads.append(name)
def installed(name): observed_reads.append(name)
root = types.ModuleType("safetensors"); root.safe_open = original
sys.modules["safetensors"] = root
def capture():
    modeling = types.ModuleType("transformers.modeling_utils")
    exec("from safetensors import safe_open", modeling.__dict__)
    sys.modules[modeling.__name__] = modeling
    return modeling
cached = capture()
root.safe_open = installed
cached.safe_open("base")
assert original_reads == ["base"] and observed_reads == []
try: v2.require_uncached_model_reader()
except ValueError: pass
else: raise AssertionError("must reject cached alias")
try: v2.verify_streaming_alias()
except ValueError: pass
else: raise AssertionError("must reject old from-import alias")
root.safe_open = original
del sys.modules["transformers.modeling_utils"]
v2.require_uncached_model_reader()
try:
    root.safe_open = installed
    fresh = capture()
    v2.verify_streaming_alias()
    fresh.safe_open("base")
finally: root.safe_open = original
assert observed_reads == ["base"] and root.safe_open is original
assert not {"torch", "peft", "PIL"} & sys.modules.keys()
print("stdlib cached alias reproduced; scope-first observed and restored")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code, str(Path(v2.__file__))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "scope-first observed and restored" in result.stdout


@pytest.mark.parametrize("missing", ["root", "modeling", "torch_alias", "modeling_alias"])
def test_scope_requires_actual_identical_safe_open_alias(monkeypatch, missing):
    root = SimpleNamespace(safe_open=lambda *a: None)
    monkeypatch.setitem(sys.modules, "safetensors", root)
    monkeypatch.setitem(sys.modules, "safetensors.torch", SimpleNamespace(safe_open=root.safe_open))
    monkeypatch.setitem(
        sys.modules, "transformers.modeling_utils", SimpleNamespace(safe_open=root.safe_open)
    )
    if missing == "root":
        monkeypatch.delitem(sys.modules, "safetensors")
    elif missing == "modeling":
        monkeypatch.delitem(sys.modules, "transformers.modeling_utils")
    elif missing == "torch_alias":
        sys.modules["safetensors.torch"].safe_open = lambda *a: None
    else:
        sys.modules["transformers.modeling_utils"].safe_open = lambda *a: None
    with pytest.raises(ValueError, match="alias"):
        v2.verify_streaming_alias()


@pytest.fixture
def plan_fixture(tmp_path, monkeypatch):
    f = r1_tests.plan_fixture.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(v2, "_r1", r1_tests.experiment)
    for name in ("safe_path", "small_record", "evaluation_api"):
        monkeypatch.setattr(v2, name, getattr(r1_tests.experiment, name))
    monkeypatch.setattr(v2, "ROOT", tmp_path)
    pins = {}
    for name in r1_tests.experiment.NEW_FILES:
        row = f.write(name, b"# sealed r1 fixture")
        pins[name] = (row["bytes"], row["sha256"])
    monkeypatch.setattr(v2, "R1_PINS", pins)
    for name in v2.NEW_FILES:
        f.write(name, b"# new v2 fixture")
    # The r1 fixture plan must record the final fixture r1 source bytes.
    f.plan = r1_tests.experiment.build_plan(f.output)
    f.binding = f.write("prior-plan.json", f.plan)
    zero = dict.fromkeys(("attempted", "returned", "synchronized", "completed"), 0)
    f.report = {
        "contract": r1_tests.experiment.CONTRACT,
        "plan": f.binding,
        "native_exit_code": 1,
        "success": False,
        "model_loaded": True,
        "head_locked_before_dev59": False,
        "optimizer_steps": zero.copy(),
        "feature_count": 0,
        "raw_prediction_count": 0,
    }
    f.receipt = {
        "contract": "multidetect.onboard-navigation-action-readout-loader-failure-evidence-v1",
        "actual_execution": {
            "actual_native_exit_code": 1,
            "worker_pid_absent": True,
            "relevant_model_processes_at_exit_check": 0,
            "output_files": ["report.json"],
        },
        "observations": {
            "success": False,
            "model_loaded": True,
            "complete_base_streaming_read_observed": False,
            "features": 0,
            "raw_predictions": 0,
            "optimizer_steps": zero.copy(),
        },
        "cumulative_budget": {
            "experiment_family": "onboard-navigation-action-readout-20261008",
            "maximum_total_head_optimizer_attempts": 800,
            "this_failed_attempt_head_optimizer_attempts": 0,
            "remaining_maximum_head_optimizer_attempts": 800,
            "inherited_base_lora_optimizer_attempts": 280,
            "inherited_base_lora_budget_reset": False,
            "new_attempt_may_not_grant_a_second_800_budget": True,
        },
    }

    def resign():
        report = f.write("failed/report.json", f.report)
        f.receipt["bindings"] = [
            f.binding,
            report,
            *(r1_tests.evaluation.record(tmp_path / name) for name in pins),
        ]
        receipt = f.write("failure-receipt.json", f.receipt)
        monkeypatch.setattr(
            v2,
            "FAILURE_PINS",
            {
                "plan": (f.binding["path"], f.binding["sha256"]),
                "report": (report["path"], report["sha256"]),
                "receipt": (receipt["path"], receipt["sha256"]),
            },
        )
        return report, receipt

    f.resign = resign
    f.resign()
    f.new_output = tmp_path / "artifacts/evaluation/onboard-navigation-action-readout-v2-fixture"
    return f


def test_v2_plan_exact_method_new_sources_closed_failure_and_no_second800(plan_fixture):
    f = plan_fixture
    plan = v2.build_plan(f.new_output)
    assert plan["contract"] == v2.CONTRACT + "-plan"
    assert plan["rule"] == f.plan["rule"] == r1_tests.experiment.RULE
    assert plan["prior_failed_attempt"]["optimizer_steps"] == dict.fromkeys(
        ("attempted", "returned", "synchronized", "completed"), 0
    )
    assert plan["prior_failed_attempt"]["remaining_maximum_attempts"] == 800
    assert plan["prior_failed_attempt"]["new_budget_granted"] is False
    assert all(
        (f.root / name).resolve() in {Path(r["path"]) for r in plan["source_files"]}
        for name in (*v2.NEW_FILES, *v2.R1_PINS)
    )
    binding = f.write("new-plan.json", plan)
    assert v2.admit_plan(binding["path"], binding["sha256"])[0] == plan
    assert not f.new_output.exists()


@pytest.mark.parametrize(
    "drift",
    [
        "spent",
        "bool_counter",
        "missing_stage",
        "feature",
        "raw",
        "head",
        "native",
        "live",
        "extra_output",
        "receipt_spent",
        "family1600",
        "remaining1600",
        "old_budget_reset",
    ],
)
def test_closed_failure_zero_spend_and_shared_budget_semantic_negatives(plan_fixture, drift):
    f = plan_fixture
    if drift == "spent":
        f.report["optimizer_steps"]["attempted"] = 1
    elif drift == "bool_counter":
        f.report["optimizer_steps"]["completed"] = False
    elif drift == "missing_stage":
        del f.report["optimizer_steps"]["returned"]
    elif drift == "feature":
        f.report["feature_count"] = 1
    elif drift == "raw":
        f.report["raw_prediction_count"] = 1
    elif drift == "head":
        f.report["head_locked_before_dev59"] = True
    elif drift == "native":
        f.report["native_exit_code"] = True
    elif drift == "live":
        f.receipt["actual_execution"]["worker_pid_absent"] = False
    elif drift == "extra_output":
        f.receipt["actual_execution"]["output_files"].append("head.safetensors")
    elif drift == "receipt_spent":
        f.receipt["observations"]["optimizer_steps"]["attempted"] = 1
    elif drift == "family1600":
        f.receipt["cumulative_budget"]["maximum_total_head_optimizer_attempts"] = 1600
    elif drift == "remaining1600":
        f.receipt["cumulative_budget"]["remaining_maximum_head_optimizer_attempts"] = 1600
    else:
        f.receipt["cumulative_budget"]["inherited_base_lora_budget_reset"] = True
    f.resign()
    with pytest.raises(ValueError, match="zero-spend.*shared800"):
        v2.build_plan(f.new_output)
    assert not f.new_output.exists()


def test_prior_receipt_or_r1_source_sha_drift_rejected_without_import(plan_fixture, monkeypatch):
    f = plan_fixture
    name = next(iter(v2.R1_PINS))
    (f.root / name).write_bytes(b"# source drift")
    monkeypatch.setattr(
        v2.importlib.util,
        "spec_from_file_location",
        lambda *a: pytest.fail("Source reject before executing sealed helpers"),
    )
    with pytest.raises(ValueError, match="byte size|SHA"):
        v2.sealed_r1()


def test_v2_plan_sha_current_source_method_mutation_and_createonly(plan_fixture):
    f = plan_fixture
    plan = v2.build_plan(f.new_output)
    plan["rule"] = plan["rule"] | {"updates_per_head": 201}
    binding = f.write("changed-plan.json", plan)
    with pytest.raises(ValueError, match="differs"):
        v2.admit_plan(binding["path"], binding["sha256"])
    plan = v2.build_plan(f.new_output)
    binding = f.write("valid-plan.json", plan)
    (f.root / v2.NEW_FILES[0]).write_bytes(b"# v2 changed")
    with pytest.raises(ValueError):
        v2.admit_plan(binding["path"], binding["sha256"])
    f.new_output.mkdir(parents=True)
    (f.new_output / "retain").write_bytes(b"retained")
    with pytest.raises(FileExistsError):
        v2.build_plan(f.new_output)
    assert (f.new_output / "retain").read_bytes() == b"retained"


def test_v2_emit_plan_createonly_python_binding_and_execute_conflict(
    plan_fixture, capsys, monkeypatch
):
    f = plan_fixture
    output = f.root / "configs/training/onboard-navigation-action-readout-v2-fixture.json"
    assert (
        v2.main(["--emit-plan", "--output-dir", str(f.new_output), "--plan-output", str(output)])
        == 0
    )
    row = json.loads(capsys.readouterr().out)
    assert row == r1_tests.evaluation.record(output)
    first = output.read_bytes()
    assert (
        v2.main(["--emit-plan", "--output-dir", str(f.new_output), "--plan-output", str(output)])
        == 1
    )
    assert output.read_bytes() == first
    monkeypatch.setattr(v2, "run", lambda *a: pytest.fail("Execute conflict must not run"))
    assert (
        v2.main(["--plan", "fixture", "--plan-sha256", "a" * 64, "--plan-output", str(output)]) == 1
    )

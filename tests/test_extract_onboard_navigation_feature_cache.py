"""Small CPU orchestration fixtures, never model/PIL/processor/GPU execution."""

from __future__ import annotations

import copy
import hashlib
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import extract_onboard_navigation_feature_cache as cache
from scripts import onboard_navigation_stable_readout as stable

torch = pytest.importorskip("torch")


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    assert not torch.cuda.is_initialized()

    def denied(*args, **kwargs):
        raise AssertionError("CPU fixtures must never initialize CUDA")

    monkeypatch.setattr(torch.cuda, "_lazy_init", denied)
    yield
    assert not torch.cuda.is_initialized()


@pytest.fixture(scope="module", autouse=True)
def one_thread():
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(previous)


def record(value, torch_module=None):
    raw = value.detach().cpu().contiguous().view(torch.uint8).numpy().tobytes()
    return {
        "dtype": str(value.dtype),
        "shape": list(value.shape),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def prefix():
    return {
        "input_ids": torch.tensor([[1, 2]]),
        "attention_mask": torch.tensor([[1, 1]]),
        "pixel_values": torch.ones(1, 3),
        "image_grid_thw": torch.tensor([[1, 1, 1]]),
    }


class Session:
    mutation = None

    def __init__(self, root):
        self.root = root
        self.verified = False
        self._active = False

    def __enter__(self):
        self._active = True
        return self

    def __exit__(self, kind, value, tb):
        if self.mutation == "exitfail":
            raise ValueError("final source SHA drift")
        self.verified = kind is None and self.mutation != "unverified"
        self._active = self.mutation == "active"
        return False

    def prime(self, samples):
        assert len(samples) == 1


@pytest.fixture
def encoding():
    fields = prefix()
    records = {key: record(value) for key, value in fields.items()}
    prepared = {"prefix_tensors": records, "actor_identity_sha256": "a" * 64}
    reference = {"actor_identity_sha256": "a" * 64}
    sample = SimpleNamespace(sample_id="s0", planned_action={"gold": "not_model_input"})

    def closed_encode(*args, session_factory, **kwargs):
        session = session_factory(cache.ROOT)
        with session:
            session.prime((sample,))
        return {
            "prefix": {key: value.clone() for key, value in fields.items()},
            "actor_identity_sha256": "a" * 64,
            "full": "host_only",
            "labels": "gold",
        }

    # Use the actual sealed expected-metadata projection, but a tiny injected
    # encoder/session. This does not certify a real source/processor.
    from scripts import experiment_onboard_navigation_action_readout as original

    env = SimpleNamespace(
        closed=SimpleNamespace(
            ClosedNavigationSourceValidationSession=Session,
            encode_closed_navigation_sample=closed_encode,
        ),
        old=SimpleNamespace(BalancedTrainingBudget=lambda: SimpleNamespace()),
        pilot=SimpleNamespace(tensor_record=record),
    )
    return sample, SimpleNamespace(processor=None, torch=torch), env, original, prepared, reference


def test_prefix_normal_closure_and_gold_full_are_not_returned(encoding):
    sample, backend, env, original, expected, reference = encoding
    result, actor, records = cache.closed_prefix(
        sample, backend, env, original, expected=expected, reference=reference
    )
    assert set(result) == stable.FOUR_PREFIX
    assert actor == reference["actor_identity_sha256"]
    assert records == expected["prefix_tensors"]
    assert "full" not in result and "labels" not in result and "gold" not in result


@pytest.mark.parametrize("mutation", ["exitfail", "unverified", "active"])
def test_failed_source_exit_cannot_return_prefix_or_forward(encoding, mutation):
    sample, backend, env, original, expected, reference = encoding
    env.closed.ClosedNavigationSourceValidationSession = type(
        "Changed", (Session,), {"mutation": mutation}
    )
    with pytest.raises(ValueError):
        cache.closed_prefix(sample, backend, env, original, expected=expected, reference=reference)


@pytest.mark.parametrize("mutation", ["extra_label", "tensor", "actor", "missing_session"])
def test_prefix_truth_leak_or_identity_changes_rejected(encoding, mutation):
    sample, backend, env, original, expected, reference = encoding
    if mutation == "missing_session":
        original = SimpleNamespace(closed_prefix=lambda *a, **k: (prefix(), "a" * 64))
    elif mutation == "tensor":
        expected = copy.deepcopy(expected)
        expected["prefix_tensors"]["input_ids"]["sha256"] = "b" * 64
    elif mutation == "actor":
        reference = {"actor_identity_sha256": "b" * 64}
    else:
        original = SimpleNamespace(closed_prefix=original.closed_prefix)
        original_encode = env.closed.encode_closed_navigation_sample

        def changed(*args, **kwargs):
            value = original_encode(*args, **kwargs)
            value["prefix"]["labels"] = torch.ones(1)
            return value

        env.closed.encode_closed_navigation_sample = changed
    with pytest.raises(ValueError):
        cache.closed_prefix(sample, backend, env, original, expected=expected, reference=reference)


def fake_api(tmp_path):
    objects = {}

    def read(row):
        return objects[row["path"]]

    def create(path, value):
        with Path(path).open("xb") as stream:
            stream.write(cache.canonical(value) + b"\n")

    return SimpleNamespace(
        STAGES=("attempted", "returned", "synchronized", "completed"),
        objects=objects,
        read_bound=read,
        create_json=create,
        record=stable.file_binding,
        bound_path=lambda row: Path(row["path"]),
        normalize=lambda rows: list({row["path"]: row for row in rows}.values()),
        capture_sources=lambda *args: {"repository": [], "runtime": []},
    )


@pytest.fixture
def runner(tmp_path, monkeypatch):
    api = fake_api(tmp_path)
    proof = tmp_path / "plan.json"
    proof.write_text('{"fixture":true}', encoding="utf-8")
    binding = stable.file_binding(proof)
    output = tmp_path / "new-output"
    ids = [f"sample-{i}" for i in range(208)]
    samples = [
        SimpleNamespace(sample_id=sid, partition="train", episode_id=f"ep-{i % 9}")
        for i, sid in enumerate(ids)
    ]
    prepared = {
        sid: {
            "prefix_tensors": {k: record(v) for k, v in prefix().items()},
            "actor_identity_sha256": "a" * 64,
        }
        for sid in ids
    }
    refs = [{"sample_id": sid, "actor_identity_sha256": "a" * 64} for sid in ids]
    api.objects.update(
        {
            "legacy": {"cpu_prefix_report": {"path": "ref86"}},
            "old": {},
            "recovery": {"output_dir": str(tmp_path / "immutable-model")},
            "prepared86": {sid: prepared[sid] for sid in ids[:86]},
            "ref86": {"samples": refs[:86]},
        }
    )
    plan = {
        "output_dir": str(output),
        "sample_ids": ids,
        "partitions": ["train"] * 208,
        "episode_ids": [sample.episode_id for sample in samples],
        "scene_ids": ["env_airsim_18"] * 86 + ["env_airsim_26"] * 122,
        "references": {"cpu122_receipt": binding},
        "cpu122_bindings": {},
        "cpu122_plan": {"optional_import_runtime": {"fixture": True}},
        "dataset_bindings": {"old86": binding, "new122": binding},
        "model_file_bindings": {"base": binding, "adapter": binding},
        "readout_plan": {
            "prepared_inputs": {"path": "prepared86"},
            "evaluation_plan": {"recovery_plan": {"path": "recovery"}, "model_budget": {}},
        },
        "runtime_sources": [{"path": str(tmp_path / "qwen3_vl/modeling_qwen3_vl.py")}],
        "expected_model_identity": {"base": "c" * 64, "adapter": "d" * 64},
    }
    order = []
    backend = SimpleNamespace(torch=torch, processor=None)

    @contextmanager
    def load_scope(legacy, old, location, loader):
        order.append("scope_enter")
        yield
        order.append("scope_exit")
        loader["model_loaded"] = True

    def load_backend(*args):
        assert order == ["fresh_reader", "scope_enter", "alias", "runtime_modules", "alias"]
        order.append("load")
        return backend

    def modules(*args):
        order.append("runtime_modules")
        return {"fake": "fixture"}

    def frozen_identity(*args):
        return plan["expected_model_identity"]

    def feature(*args, **kwargs):
        assert set(args[1]) == stable.FOUR_PREFIX
        return torch.ones(1, 2048), 0.001

    readout = SimpleNamespace(
        admit_runtime=lambda *a: {"scope": "fixture_not_actual_runtime"},
        require_uncached_model_reader=lambda: order.append("fresh_reader"),
        verify_streaming_alias=lambda: order.append("alias"),
        verify_runtime_modules=modules,
        freeze_backend=frozen_identity,
        parameter_identity=frozen_identity,
        check_frozen=lambda *a: None,
        extract_feature=feature,
    )

    @contextmanager
    def isolation(observation):
        yield
        observation.update(own_finder_removed=True, prior_finders_preserved=True)

    cpu = SimpleNamespace(
        ISOLATION_POLICY={"fixture": True},
        OptionalArrowIsolation=isolation,
        validate_optional_origins=lambda *a: {},
        base=SimpleNamespace(assert_no_forbidden_modules=lambda: None),
    )
    env = SimpleNamespace(
        old=SimpleNamespace(loading_scope=load_scope, load_backend=load_backend),
        recovery=SimpleNamespace(
            RecoveryModelBudget=lambda **k: SimpleNamespace(validate=lambda: None),
            check_loader_cleanup=lambda *a: order.append("cleanup"),
        ),
        pilot=SimpleNamespace(tensor_record=record),
    )
    controls = SimpleNamespace(api=api, readout=readout, cpu=cpu, stable=stable)
    monkeypatch.setattr(cache, "admit_plan", lambda *args: (controls, plan, binding))
    monkeypatch.setattr(cache, "fresh_output", lambda value, readout: Path(value))
    monkeypatch.setattr(cache, "environment", lambda api: env)
    monkeypatch.setattr(
        cache,
        "sample_inputs",
        lambda *a: (
            samples,
            api.objects["legacy"],
            {},
            {sid: prepared[sid] for sid in ids[86:]},
            refs[86:],
        ),
    )
    monkeypatch.setattr(cache, "actual_runtime_origins", lambda *a: {"scope": "fixture"})
    monkeypatch.setattr(
        cache,
        "closed_prefix",
        lambda *a, **kw: (prefix(), "a" * 64, kw["expected"]["prefix_tensors"]),
    )
    # The actual head module is imported only after scope cleanup, just as the
    # production path requires. It is a component, not a model or optimizer.
    return SimpleNamespace(
        api=api,
        plan=plan,
        controls=controls,
        env=env,
        order=order,
        backend=backend,
        binding=binding,
        output=output,
        prepared=prepared,
        samples=samples,
    )


def test_full208_fixture_persists_exact_cache_manifest_and_false_claims(runner):
    result = cache.run("fixture", "a" * 64)
    assert (
        result["success"] and result["native_exit_code"] == 0 and result["feature_cache_admitted"]
    )
    assert result["feature_count"] == result["fused_prefill_attempts"] == 208
    assert result["source_validation_sessions_verified"] == 208
    assert result["cache_tensor_bytes"] == 1703936
    assert result["optimizer_steps"] == dict.fromkeys(runner.api.STAGES, 0)
    assert result["source_sessions_verified"] is True
    assert result["all208_prefix_metadata_equal"] is True
    assert result["cache_saved_and_exactly_reloaded"] is True
    assert result["inherited_head_optimizer_attempts"] == 800
    assert result["inherited_base_lora_optimizer_attempts"] == 280
    assert all(value is False for value in result["claims"].values())
    assert runner.order.index("fresh_reader") < runner.order.index("scope_enter")
    assert runner.order.index("scope_exit") < runner.order.index("cleanup")
    assert (runner.output / "manifest.json").is_file()
    loaded, metadata = stable.load_feature_cache(
        runner.output / "cache",
        receipt_sha256=result["cache_receipt"]["sha256"],
        expected_sample_ids=runner.plan["sample_ids"],
        expected_model_identity=runner.plan["expected_model_identity"],
    )
    assert torch.equal(loaded, torch.ones(208, 2048))
    assert metadata["partitions"] == ["train"] * 208
    assert metadata["sample_ids"] == runner.plan["sample_ids"]


@pytest.mark.parametrize(
    "mutation",
    ["wrong_model", "nan", "shape", "order", "session", "final_digest", "late_source", "cleanup"],
)
def test_failclosed_partial_report_never_admits_cache(runner, monkeypatch, mutation):
    if mutation == "wrong_model":
        runner.controls.readout.freeze_backend = lambda *a: {"base": "e" * 64, "adapter": "d" * 64}
    elif mutation in {"nan", "shape"}:
        runner.controls.readout.extract_feature = lambda *a, **k: (
            torch.full((1, 2048), float("nan")) if mutation == "nan" else torch.ones(1, 2),
            0.01,
        )
    elif mutation == "order":
        runner.samples.reverse()
    elif mutation == "session":

        def bad(*args, **kwargs):
            raise ValueError("source normal exit failed")

        monkeypatch.setattr(cache, "closed_prefix", bad)
    elif mutation == "final_digest":
        runner.controls.readout.parameter_identity = lambda *a: {
            "base": "e" * 64,
            "adapter": "d" * 64,
        }
    elif mutation == "late_source":
        calls = 0

        def capture(*args):
            nonlocal calls
            calls += 1
            return {"repository": [], "runtime": [] if calls < 3 else [{"changed": True}]}

        runner.api.capture_sources = capture
    else:

        @contextmanager
        def failed_cleanup(observation):
            yield
            raise ValueError("owned import finder cleanup failed")

        runner.controls.cpu.OptionalArrowIsolation = failed_cleanup
    result = cache.run("fixture", "a" * 64)
    assert not result["success"] and not result["feature_cache_admitted"]
    assert result["native_exit_code"] == 1 and result["partial_failures"]
    assert all(value is False for value in result["claims"].values())
    assert result["new_optimizer_attempts"] == 0
    if mutation in {"late_source", "cleanup"}:
        assert result["feature_count"] == 208
        assert (runner.output / "cache" / "receipt.json").exists()
    else:
        assert not (runner.output / "cache").exists()
    if mutation == "session":
        assert result["fused_prefill_attempts"] == 0
    if mutation in {"nan", "shape"}:
        assert result["fused_prefill_attempts"] == 1 and result["feature_count"] == 0


def test_create_only_existing_output_never_overwrites(runner):
    runner.output.mkdir()
    sentinel = runner.output / "report.json"
    sentinel.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(FileExistsError):
        cache.run("fixture", "a" * 64)
    assert sentinel.read_text(encoding="utf-8") == "do not overwrite"


def test_actual_returned_backend_is_recorded_on_loader_cleanup_failure(runner):
    def fail(*args):
        raise ValueError("real loader cleanup failure")

    runner.env.recovery.check_loader_cleanup = fail
    result = cache.run("fixture", "a" * 64)
    assert result["model_loaded"] is True
    assert result["feature_count"] == result["fused_prefill_attempts"] == 0
    assert result["success"] is result["feature_cache_admitted"] is False
    assert result["source_sessions_verified"] is False
    assert result["all208_prefix_metadata_equal"] is False
    assert result["cache_saved_and_exactly_reloaded"] is False


@pytest.mark.parametrize("mutation", ["wrong_origin", "late_drift"])
def test_optional_actual_import_origin_failure_cannot_seal_cache(runner, mutation):
    calls = 0

    def origins(*args):
        nonlocal calls
        calls += 1
        if mutation == "wrong_origin":
            raise ValueError("Actual optional Python module origin differs")
        return {} if calls == 1 else {"pandas": "unexpected.py"}

    runner.controls.cpu.validate_optional_origins = origins
    result = cache.run("fixture", "a" * 64)
    assert result["success"] is result["feature_cache_admitted"] is False
    assert result["partial_failures"] and result["native_exit_code"] == 1
    assert result["feature_count"] == (0 if mutation == "wrong_origin" else 208)
    assert not (runner.output / "cache").exists()


@pytest.fixture
def successful_cpu122(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "ROOT", tmp_path)
    actual_root = tmp_path / "artifacts/diagnostics/onboard-navigation-indexed-cpu122-20261008-r2"
    paths = {
        "report": actual_root / "report.json",
        "plan": tmp_path / "configs/training/onboard-navigation-indexed-cpu122-20261008-r2.json",
        "prepared": actual_root / "prepared-inputs.json",
        "prefix": actual_root / "prefix-reference.json",
    }
    bindings = {
        name: {"path": str(path), "bytes": 123, "sha256": digit * 64}
        for digit, (name, path) in zip("abcd", paths.items(), strict=True)
    }
    ids = [f"n-{i}" for i in range(122)]
    plan = {"sample_ids": ids, "sources": [{"fixture_source": True}]}
    prepared = {sid: {"fixture_reference": True} for sid in ids}
    prefix_rows = [{"sample_id": sid} for sid in ids]
    cpu = SimpleNamespace(
        CONTRACT="fixture-successful-cpu122",
        base=SimpleNamespace(LIMITS={"cpu_threads": 1}, FALSE_CLAIMS=("model_loaded",)),
    )
    report = {
        "contract": cpu.CONTRACT,
        "native_exit_code": 0,
        "success": True,
        "training_dataset_cpu_admitted": True,
        "sample_count": 122,
        "source_validation_sessions_verified": 122,
        "partial_failures": [],
        "saved_cpu122_metadata_exact": True,
        "source_bindings_unchanged": True,
        "source_bindings_before": plan["sources"],
        "source_bindings_after": plan["sources"],
        "plan": bindings["plan"],
        "prepared_inputs": bindings["prepared"],
        "prefix_reference": bindings["prefix"],
        "admitted_sample_ids": ids,
        "limits": cpu.base.LIMITS,
        "optional_arrow_isolation": {
            "own_finder_removed": True,
            "prior_finders_preserved": True,
        },
        "model_loaded": False,
    }
    objects = {
        bindings["report"]["path"]: report,
        bindings["plan"]["path"]: plan,
        bindings["prepared"]["path"]: prepared,
        bindings["prefix"]["path"]: {"samples": prefix_rows},
    }
    api = SimpleNamespace(normalize=lambda rows: rows, read_bound=lambda row: objects[row["path"]])
    receipt = {
        "contract": "multidetect.onboard-navigation-indexed-cpu122-admission-evidence-v2",
        "actual_execution": {"actual_native_exit_code": 0, "worker_pid_absent": True},
        "observations": {
            "success": True,
            "training_dataset_cpu_admitted": True,
            "sample_count": 122,
            "source_bindings_unchanged": True,
        },
        "actual_execution_third_CPU122_run_claimed": False,
        "claims": {"quality_claim": False},
        "bindings": list(bindings.values()),
    }
    return SimpleNamespace(
        api=api,
        cpu=cpu,
        receipt=receipt,
        report=report,
        plan=plan,
        prepared=prepared,
        references=prefix_rows,
        bindings=bindings,
    )


def test_exact_resolved_cpu122_paths_accept_native_windows_normalization(successful_cpu122):
    f = successful_cpu122
    plan, prepared, references, _, bindings = cache.cpu122_inputs(f.receipt, f.api, f.cpu)
    assert plan is f.plan and prepared is f.prepared and references is f.references
    assert bindings == f.bindings
    if sys.platform == "win32":
        assert "\\" in bindings["report"]["path"]


@pytest.mark.parametrize("mutation", ["same_name_wrong_root", "source_drift", "metadata_failed"])
def test_cpu122_report_and_full_authority_identity_rejections(successful_cpu122, mutation):
    f = successful_cpu122
    if mutation == "same_name_wrong_root":
        # A report with the same basename or an enticing suffix is not authority.
        f.receipt["bindings"][0] = {
            **f.receipt["bindings"][0],
            "path": f.bindings["report"]["path"].replace("artifacts", "wrong-artifacts"),
        }
    elif mutation == "source_drift":
        f.report["source_bindings_after"] = [{"fixture_source": False}]
    else:
        f.report["saved_cpu122_metadata_exact"] = False
    with pytest.raises(ValueError):
        cache.cpu122_inputs(f.receipt, f.api, f.cpu)


@pytest.mark.parametrize("mutation", [None, "native_origin", "processor_class_origin"])
def test_actual_runtime_native_and_processor_class_origins(tmp_path, monkeypatch, mutation):
    runtime = tmp_path / "pinned-runtime.py"
    processor = type("Qwen3VLProcessor", (), {})()
    processor.image_processor = SimpleNamespace()
    processor.tokenizer = SimpleNamespace()
    backend = SimpleNamespace(processor=processor)
    api = SimpleNamespace(verify=lambda *a, **kw: runtime)
    plan = {"runtime_sources": [{"fixture_bound": True}]}

    def imported(name):
        path = tmp_path / "wrong-native.py" if name == "torch._C" and mutation else runtime
        if mutation != "native_origin":
            path = runtime
        return SimpleNamespace(__file__=str(path))

    monkeypatch.setattr(cache.importlib, "import_module", imported)
    monkeypatch.setattr(
        cache.inspect,
        "getsourcefile",
        lambda value: (
            str(tmp_path / "wrong-class.py")
            if mutation == "processor_class_origin"
            else str(runtime)
        ),
    )
    if mutation:
        with pytest.raises(ValueError, match="origin is unbound"):
            cache.actual_runtime_origins(plan, backend, api)
    else:
        origins = cache.actual_runtime_origins(plan, backend, api)
        assert "torch._C" in origins and "tokenizers.tokenizers" in origins
        assert set(origins.values()) == {str(runtime.resolve())}


def test_prefill_budget_never_exceeded(runner):
    runner.plan["sample_ids"].append("extra")
    samples = runner.samples + [SimpleNamespace(sample_id="extra")]
    expected = runner.prepared | {"extra": runner.prepared[runner.samples[0].sample_id]}
    refs = {sid: {"actor_identity_sha256": "a" * 64} for sid in expected}
    report = {"feature_count": 0, "fused_prefill_attempts": 0}
    runner.env.head_api = None
    with pytest.raises(ValueError, match="budget exhausted"):
        cache.extract_rows(
            samples,
            runner.backend,
            runner.env,
            runner.controls,
            runner.plan,
            report,
            expected,
            refs,
        )
    assert report["fused_prefill_attempts"] == 208 and report["feature_count"] == 208


@pytest.mark.parametrize("mutation", ["success", "count", "native", "claims", "third_run"])
def test_cpu_receipt_cannot_admit_missing_or_failed122(mutation):
    receipt = {
        "contract": "multidetect.onboard-navigation-indexed-cpu122-admission-evidence-v2",
        "actual_execution": {"actual_native_exit_code": 0, "worker_pid_absent": True},
        "observations": {
            "success": True,
            "training_dataset_cpu_admitted": True,
            "sample_count": 122,
            "source_bindings_unchanged": True,
        },
        "actual_execution_third_CPU122_run_claimed": False,
        "claims": {"quality_claim": False},
    }
    if mutation == "success":
        receipt["observations"]["success"] = False
    elif mutation == "count":
        receipt["observations"]["sample_count"] = 121
    elif mutation == "native":
        receipt["actual_execution"]["actual_native_exit_code"] = False
    elif mutation == "claims":
        receipt["claims"]["quality_claim"] = True
    else:
        receipt["actual_execution_third_CPU122_run_claimed"] = True
    with pytest.raises(ValueError, match="successful CPU122"):
        cache.cpu122_inputs(receipt, SimpleNamespace(), SimpleNamespace())


def test_wrong_explicit_plan_sha_fails_before_model_import(monkeypatch):
    api = SimpleNamespace(
        explicit_binding=lambda *a: (_ for _ in ()).throw(ValueError("SHA drift"))
    )
    monkeypatch.setattr(
        cache,
        "bootstrap",
        lambda: SimpleNamespace(api=api, readout=SimpleNamespace(safe_path=lambda value: value)),
    )
    with pytest.raises(ValueError, match="SHA drift"):
        cache.admit_plan("plan", "f" * 64)


def test_dependency_drift_rejected_before_import_execution(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    path = tmp_path / "scripts" / "dependency.py"
    path.write_bytes(b"changed")
    monkeypatch.setattr(cache, "ROOT", tmp_path)
    monkeypatch.setattr(cache, "PINS", {"scripts/dependency.py": (7, "a" * 64)})

    def forbidden(*args):
        raise AssertionError("Unauthenticated dependency may not execute")

    monkeypatch.setattr(cache.importlib, "import_module", forbidden)
    with pytest.raises(ValueError, match="dependency drift"):
        cache.bootstrap()


def test_import_and_help_are_stdlib_only():
    root = Path(__file__).resolve().parents[1]
    code = (
        f"import sys;sys.path.insert(0,{str(root)!r});"
        "from scripts import extract_onboard_navigation_feature_cache as c;"
        "assert not {'torch','numpy','transformers','peft','PIL','safetensors'} & "
        "{name.split('.')[0] for name in sys.modules}"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", str(root / cache.NEW_FILES[0]), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and "--plan-sha256" in result.stdout


def test_cli_argument_mixing_denied_before_bootstrap(monkeypatch):
    monkeypatch.setattr(
        cache, "bootstrap", lambda: (_ for _ in ()).throw(AssertionError("must not bootstrap"))
    )
    assert cache.main(["--plan", "x", "--plan-sha256", "a" * 64, "--output-dir", "y"]) == 1

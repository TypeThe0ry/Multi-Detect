"""CPU tiny tensors and synthetic metadata only; no official/model/GPU evidence."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")

from multidetect.autonomy import onboard_navigation_inference as inference  # noqa: E402
from scripts import evaluate_onboard_navigation_original82 as evaluation  # noqa: E402
from scripts import experiment_onboard_navigation_action_readout as experiment  # noqa: E402
from scripts import onboard_navigation_action_readout as readout  # noqa: E402


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    assert not torch.cuda.is_initialized()
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda *a, **k: pytest.fail("CUDA forbidden"))
    previous = torch.get_num_threads()
    torch.set_num_threads(1)
    yield
    torch.set_num_threads(previous)
    assert not torch.cuda.is_initialized()


def tensor_record(tensor, _torch=torch):
    return {
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype),
        "sha256": hashlib.sha256(
            tensor.detach().cpu().contiguous().view(torch.uint8).numpy().tobytes()
        ).hexdigest(),
    }


def sample(sid, episode, step, category="forward", partition="train"):
    if category == "forward":
        action = SimpleNamespace(
            kind="translate", translation_m=(3.0, 0.0, 0.0), yaw_delta_rad=None
        )
    elif category in {"up", "down"}:
        action = SimpleNamespace(
            kind="translate",
            translation_m=(0.0, 0.0, -3.0 if category == "up" else 3.0),
            yaw_delta_rad=None,
        )
    elif category.startswith("yaw"):
        action = SimpleNamespace(
            kind="turn_yaw",
            translation_m=None,
            yaw_delta_rad=-0.5 if category == "yaw_left" else 0.5,
        )
    elif category == "stop":
        action = SimpleNamespace(kind="stop", translation_m=None, yaw_delta_rad=None)
    else:
        raise AssertionError(category)
    return SimpleNamespace(
        sample_id=sid,
        episode_id=episode,
        step_index=step,
        partition=partition,
        planned_action=action,
    )


def old_samples():
    return tuple(
        sample(
            f"old-{n}",
            f"episode-{n // 30}",
            n % 30,
            ("forward", "yaw_left", "yaw_right", "stop")[n % 4],
        )
        for n in range(86)
    )


def prefix():
    return {
        "input_ids": torch.tensor([[11, 22, 0]], dtype=torch.int64),
        "attention_mask": torch.tensor([[1, 1, 0]], dtype=torch.int64),
        "image_grid_thw": torch.tensor([[1, 2, 2]], dtype=torch.int64),
        "pixel_values": torch.zeros(4, 3, dtype=torch.float32),
    }


class Qwen3VLModel(torch.nn.Module):
    """Deliberate CPU stand-in, never an official-model acceptance fixture."""

    def __init__(self):
        super().__init__()
        self.weight = torch.nn.Parameter(torch.tensor([2.0]))
        self.lora_A = torch.nn.ModuleDict({"default": torch.nn.Linear(1, 1, bias=False)})
        self.lora_B = torch.nn.ModuleDict({"default": torch.nn.Linear(1, 1, bias=False)})
        self.disable_adapters = False
        self.active_adapters = ["default"]
        self.calls = []
        self.after = lambda: None

    def forward(self, **kwargs):
        assert not torch.is_grad_enabled()
        self.calls.append(kwargs)
        hidden = torch.zeros(1, 3, 2048, dtype=torch.float32)
        hidden[:, 0] = 1
        hidden[:, 1] = 2
        hidden[:, 2] = 999  # mask position2 must not be chosen
        self.after()
        return SimpleNamespace(last_hidden_state=hidden)


class Model(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.model = Qwen3VLModel()
        self.active_adapters = ["default"]

    def get_base_model(self):
        return self

    def generate(self, *args, **kwargs):
        pytest.fail("No autoregressive token generation")


def frozen_backend():
    model = Model()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    model.eval()
    return SimpleNamespace(model=model, torch=torch, processor=object())


def test_exact_episode_folds_no_adjacent_random_split():
    samples = old_samples()
    folds = experiment.episode_folds(samples)
    assert len(folds) == 3
    assert sorted(sid for f in folds for sid in f["heldout_ids"]) == sorted(
        s.sample_id for s in samples
    )
    for fold in folds:
        assert set(fold["train_ids"]).isdisjoint(fold["heldout_ids"])
        assert {s.episode_id for s in samples if s.sample_id in fold["heldout_ids"]} == {
            fold["episode_id"]
        }


@pytest.mark.parametrize("change", ["duplicate", "missing", "four_episodes", "one_row_episode"])
def test_fold_coverage_rejects_changes(change):
    samples = list(old_samples())
    if change == "duplicate":
        samples[-1] = samples[0]
    elif change == "missing":
        samples.pop()
    elif change == "four_episodes":
        samples[-1].episode_id = "fourth"
    else:
        for s in samples:
            s.episode_id = "first" if s.step_index % 2 else "second"
        samples[-1].episode_id = "third"
    with pytest.raises(ValueError):
        experiment.episode_folds(samples)


def test_permutation_is_within_episode_derangement_not_new_training():
    ids = ["a", "b", "c", "d"]
    result = experiment.permuted_ids(ids, 20261008)
    assert sorted(result) == ids and all(a != b for a, b in zip(ids, result, strict=True))
    assert result == experiment.permuted_ids(ids, 20261008)
    with pytest.raises(ValueError):
        experiment.permuted_ids(["a"], 1)


def test_actual_small_feature_forward_four_inputs_no_labels_no_cache_last_nonzero():
    backend = frozen_backend()
    inputs = prefix()
    feature, elapsed = experiment.extract_feature(backend, inputs, readout)
    assert feature.shape == (1, 2048) and feature.dtype == torch.float32
    assert feature.device.type == "cpu" and not feature.requires_grad
    assert torch.equal(feature, torch.full((1, 2048), 2.0))
    kwargs = backend.model.model.calls[0]
    assert set(kwargs) == set(inputs) | {
        "use_cache",
        "past_key_values",
        "return_dict",
        "output_hidden_states",
    }
    assert kwargs["use_cache"] is False and kwargs["past_key_values"] is None
    assert kwargs["output_hidden_states"] is False and kwargs["return_dict"] is True
    assert "labels" not in kwargs and elapsed >= 0
    assert all(p.grad is None and not p.requires_grad for p in backend.model.parameters())


@pytest.mark.parametrize(
    "change",
    [
        "extra_gold",
        "wrong_class",
        "bad_shape",
        "nonfinite",
        "train_mode",
        "unfrozen",
        "disabled_lora",
        "post_lora",
    ],
)
def test_feature_forward_rejects_interface_and_state_drift(change):
    backend = frozen_backend()
    inputs = prefix()
    if change == "extra_gold":
        inputs["labels"] = torch.ones(1, 3)
    elif change == "wrong_class":
        backend.model.model = torch.nn.Linear(2, 2)
    elif change in {"bad_shape", "nonfinite"}:
        value = torch.zeros(1, 3, 7 if change == "bad_shape" else 2048)
        if change == "nonfinite":
            value[0, 0, 0] = float("nan")
        backend.model.model.forward = lambda **kwargs: SimpleNamespace(last_hidden_state=value)
    elif change == "train_mode":
        backend.model.train()
    elif change == "unfrozen":
        next(backend.model.parameters()).requires_grad_(True)
    elif change == "disabled_lora":
        backend.model.model.disable_adapters = True
    else:
        backend.model.model.after = lambda: setattr(backend.model.model, "disable_adapters", True)
    with pytest.raises(ValueError):
        experiment.extract_feature(backend, inputs, readout)


def test_runtime_class_file_binding_checked_before_forward():
    backend = frozen_backend()
    with pytest.raises(ValueError, match="bound runtime"):
        experiment.extract_feature(backend, prefix(), readout, expected_runtime_path="wrong.py")
    assert not backend.model.model.calls


def test_labels_only_old_supported_actions_missing_scales_stay_missing():
    env = SimpleNamespace(eval=evaluation, inference=inference)
    samples = [sample("f", "a", 0), sample("l", "a", 1, "yaw_left"), sample("s", "a", 2, "stop")]
    samples[1].planned_action.yaw_delta_rad = None
    with pytest.raises(ValueError, match="Unsupported"):
        experiment.labels_for(samples, env, readout)
    labels = experiment.labels_for([samples[0], samples[2]], env, readout)
    assert labels[0].distance_m == 3 and labels[0].angle_magnitude_rad is None
    assert labels[1].distance_m is None and labels[1].angle_magnitude_rad is None
    for direction in ("up", "down"):
        with pytest.raises(ValueError, match="Unsupported"):
            experiment.labels_for([sample("v", "a", 1, direction)], env, readout)


def test_two_real_cpu_head_updates_ledger_detached_features_and_fixed_parameter_count():
    features = torch.randn(4, 2048).detach()
    original = features.clone()
    labels = [readout.ActionReadoutLabel(name) for name in readout.CLASS_NAMES]
    ledger = dict.fromkeys(("attempted", "returned", "synchronized", "completed"), 0)
    stream = io.BytesIO()
    # BytesIO deliberately has no fsync descriptor: only the fsync boundary is injected.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(experiment.os, "fsync", lambda fd: None)

        class MemoryLog(io.BytesIO):
            def fileno(self):
                return 0

        stream = MemoryLog()
        head, record = experiment.fit_head(
            features,
            labels,
            head_api=readout,
            torch=torch,
            seed=7,
            updates=2,
            stream=stream,
            ledger=ledger,
            head_id="tiny",
        )
    assert ledger == dict.fromkeys(ledger, 2)
    assert sum(p.numel() for p in head.parameters()) == 12294
    assert record["initial_parameter_sha256"] != record["final_parameter_sha256"]
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [e["stage"] for e in events] == [
        "attempted",
        "returned",
        "synchronized",
        "completed",
    ] * 2
    assert torch.equal(features, original) and features.grad is None


@pytest.mark.parametrize("updates", [0, 201, True, 1.0])
def test_head_update_budget_strict_integer(updates):
    with pytest.raises(ValueError, match="At most200"):
        experiment.fit_head(
            torch.zeros(1, 2048),
            [readout.ActionReadoutLabel("forward")],
            head_api=readout,
            torch=torch,
            seed=1,
            updates=updates,
            stream=io.BytesIO(),
            ledger={},
            head_id="x",
        )


def test_head_step_failure_consumes_attempt_without_return_or_reset(tmp_path, monkeypatch):
    ledger = dict.fromkeys(("attempted", "returned", "synchronized", "completed"), 0)
    monkeypatch.setattr(
        torch.optim.SGD, "step", lambda self: (_ for _ in ()).throw(MemoryError("step"))
    )
    with (tmp_path / "wal").open("xb") as stream, pytest.raises(MemoryError):
        experiment.fit_head(
            torch.zeros(1, 2048),
            [readout.ActionReadoutLabel("forward")],
            head_api=readout,
            torch=torch,
            seed=1,
            updates=1,
            stream=stream,
            ledger=ledger,
            head_id="x",
        )
    assert ledger == {"attempted": 1, "returned": 0, "synchronized": 0, "completed": 0}
    assert len((tmp_path / "wal").read_bytes().splitlines()) == 1


def test_sidecar_actual_save_reload_bitwise_identity_create_only(tmp_path):
    head = readout.NavigationActionReadout()
    clone, bindings = experiment.save_reload_head(
        head, tmp_path / "head", {"fixture": True}, readout, torch, evaluation
    )
    assert bindings["reloaded_parameter_sha256"] == experiment.head_digest(head, torch)
    assert all(not p.requires_grad for p in clone.parameters())
    for name, value in head.state_dict().items():
        assert torch.equal(clone.state_dict()[name], value)
    with pytest.raises(FileExistsError):
        experiment.save_reload_head(head, tmp_path / "head", {}, readout, torch, evaluation)


def test_raw_selected_scale_rejection_keeps_original_values_no_generated_tokens():
    head = readout.NavigationActionReadout()
    with torch.no_grad():
        head.linear.weight.zero_()
        head.linear.bias.copy_(torch.tensor([4.0, 0.0, 0.0, 0.0, -1.0, 0.5]))
    observation = {"sample_id": "other", "prefix_tensors": {"fixture": "a"}}
    bindings = {
        name: {"sha256": "a" * 64}
        for name in ("base_weights", "adapter_weights", "weights", "config")
    }
    row = experiment.predict_head(head, "a", torch.zeros(1, 2048), observation, bindings, readout)
    assert row["selected_scale_rejected"] is True
    assert row["raw"]["raw_distance_m"] == -1 and "target" not in row["raw"]
    assert row["feature_source_sample_id"] == "other" and row["text_generation_performed"] is False
    assert "output_token_ids" not in row and "generated_tokens" not in row
    assert row["raw"]["actual_model_identity_verified"] is False


@pytest.fixture
def orchestration(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    samples = old_samples()
    dev = tuple(
        sample(
            f"dev-{n}",
            "dev-a" if n < 17 else "dev-b",
            n,
            "up" if n == 54 else "down" if n == 55 else "stop" if n in (16, 58) else "forward",
            "dev-validation" if n < 17 else "dev-test",
        )
        for n in range(59)
    )
    output = tmp_path / "artifacts/evaluation/onboard-navigation-action-readout-fixture"
    output.parent.mkdir(parents=True)
    backend = frozen_backend()
    identities = {"base": "b" * 64, "adapter": "c" * 64}
    monkeypatch.setattr(experiment, "parameter_identity", lambda *args: identities.copy())
    legacy = {"original_plan": "old", "cpu_prefix_report": "prefix"}
    inherited = {"recovery_plan": "recovery", "model_budget": {}, "dataset_report": "bridge"}
    records = {k: tensor_record(v) for k, v in prefix().items()}
    prepared = {s.sample_id: {"prefix_tensors": records} for s in samples}
    plan = {
        "output_dir": str(output),
        "rule": experiment.RULE.copy(),
        "legacy_plan": "legacy",
        "evaluation_plan": inherited,
        "prepared_inputs": "prepared",
        "folds": experiment.episode_folds(samples),
        "old86_ids": [s.sample_id for s in samples],
        "train_derived_forward_reference_target": evaluation.gold_mapping(samples[0]),
        "dev59_ids": [s.sample_id for s in dev],
        "dev_actor_identities": {s.sample_id: "d" * 64 for s in dev},
        "expected_base_parameter_sha256": identities["base"],
        "expected_adapter_parameter_sha256": identities["adapter"],
        "runtime_sources": [{"path": "/fixture/qwen3_vl/modeling_qwen3_vl.py"}],
        "base_weights": {"sha256": "b" * 64},
        "adapter_weights": {"sha256": "c" * 64},
    }
    maps = {
        "legacy": legacy,
        "old": {"dataset_samples": "samples"},
        "prepared": prepared,
        "prefix": {"samples": [{"sample_id": s.sample_id} for s in samples]},
        "recovery": {"output_dir": str(tmp_path / "old-run")},
        "bridge": {},
    }
    calls, captures = [], []
    budget = SimpleNamespace(validate=lambda: None)
    env = SimpleNamespace(
        pilot=SimpleNamespace(tensor_record=tensor_record),
        inference=inference,
        old=SimpleNamespace(
            loading_scope=lambda *a: __import__("contextlib").nullcontext(),
            load_backend=lambda *a: a[-1].update(model_loaded=True) or backend,
        ),
        recovery=SimpleNamespace(
            RecoveryModelBudget=lambda **k: budget, check_loader_cleanup=lambda *a: None
        ),
    )

    def capture(*args):
        captures.append("capture")
        return {"fixture": "same"}

    def score(raw, gold, infer):
        assert len((output / "predictions.jsonl").read_bytes().splitlines()) == 231
        calls.append("score")
        return evaluation.score_prediction(raw, gold, infer)

    api = SimpleNamespace(
        apis=lambda: env,
        read_bound=lambda binding: maps[binding],
        capture_sources=capture,
        create_json=evaluation.create_json,
        append_row=evaluation.append_row,
        record=evaluation.record,
        verify=lambda row: Path(row["path"]),
        gold_mapping=evaluation.gold_mapping,
        score_prediction=score,
        primitive_class=evaluation.primitive_class,
        strict_json=evaluation.strict_json,
    )
    monkeypatch.setattr(experiment, "admit_plan", lambda *a: (plan, {"sha256": "a" * 64}, api))
    monkeypatch.setattr(experiment, "load_samples", lambda *a: samples)
    monkeypatch.setattr(experiment, "admit_runtime", lambda *a: {"CPU_fixture_only": True})
    monkeypatch.setattr(experiment, "verify_runtime_modules", lambda *a: {"CPU_fixture_only": True})
    monkeypatch.setattr(api, "admit_dataset", lambda *a: ((), dev, {}), raising=False)

    def encode(s, *args, **kwargs):
        if s.sample_id.startswith("dev"):
            assert (output / "final86/head.safetensors").is_file()
            assert (output / "final86/config.json").is_file()
            calls.append("dev_extract")
        else:
            assert kwargs["expected"] is prepared[s.sample_id]
            calls.append("old_extract")
        return prefix(), "d" * 64

    monkeypatch.setattr(experiment, "closed_prefix", encode)
    monkeypatch.setattr(experiment, "extract_feature", lambda *a, **k: (torch.zeros(1, 2048), 0.01))

    def fit(features, labels, **kwargs):
        assert "dev_extract" not in calls and kwargs["updates"] == 200
        calls.append("fit")
        for stage in kwargs["ledger"]:
            kwargs["ledger"][stage] += 200
        head = readout.NavigationActionReadout()
        with torch.no_grad():
            head.linear.weight.zero_()
            head.linear.bias.copy_(torch.tensor([4.0, 0.0, 0.0, 0.0, 3.0, 0.5]))
        return head, {
            "initial_parameter_sha256": "e" * 64,
            "final_parameter_sha256": experiment.head_digest(head, torch),
            "updates": 200,
        }

    monkeypatch.setattr(experiment, "fit_head", fit)
    monkeypatch.setattr(
        experiment, "compare_generative_reference", lambda *a: {"fixture_only": True}
    )
    return SimpleNamespace(
        plan=plan,
        api=api,
        calls=calls,
        captures=captures,
        output=output,
        backend=backend,
        samples=samples,
        dev=dev,
    )


def test_full145_feature_231_raw_800budget_orchestration_gold_last_no_old_writes(orchestration):
    fixture = orchestration
    result = experiment.run(SimpleNamespace(plan="fixture", plan_sha256="a" * 64))
    assert result["native_exit_code"] == 0 and result["success"] is True
    assert result["feature_count"] == 145 and result["raw_prediction_count"] == 231
    assert result["optimizer_steps"] == dict.fromkeys(result["optimizer_steps"], 800)
    assert fixture.calls[:86] == ["old_extract"] * 86
    assert fixture.calls[86:90] == ["fit"] * 4
    assert fixture.calls[90:149] == ["dev_extract"] * 59
    assert fixture.calls[149:] == ["score"] * 290
    assert result["score_counts"]["locked_final86_dev59"]["rows"] == 59
    assert result["score_counts"]["locked_final86_dev59"]["unsupported"] == 2
    assert result["dev_partition_scores"]["dev-validation"]["rows"] == 17
    assert result["dev_partition_scores"]["dev-test"]["rows"] == 42
    assert all(row["saved_reload_raw_outputs_exact"] is True for row in result["heads"].values())
    assert len(fixture.captures) == 2
    assert result["source_bindings_unchanged"] is True
    assert all(value is False for value in result["claims"].values())
    rows = [
        json.loads(line)
        for line in (fixture.output / "predictions.jsonl").read_bytes().splitlines()
    ]
    assert all("gold" not in r and "score" not in r and "generated_tokens" not in r for r in rows)
    assert all(r["sample_id"].startswith("dev-") for r in rows[-59:])
    assert not (fixture.output.parents[2] / "old-run").exists()


@pytest.mark.parametrize(
    "failure", ["old_close", "dev_close", "source_after", "identity_after", "raw_fsync"]
)
def test_orchestration_failures_durable_no_retry_or_success(orchestration, monkeypatch, failure):
    fixture = orchestration
    if failure in {"old_close", "dev_close"}:
        old = experiment.closed_prefix

        def fail(s, *a, **k):
            if s.sample_id.startswith("dev") == (failure == "dev_close"):
                raise MemoryError("final source exit")
            return old(s, *a, **k)

        monkeypatch.setattr(experiment, "closed_prefix", fail)
    elif failure == "source_after":
        monkeypatch.setattr(fixture.api, "capture_sources", lambda *a: {"call": len(fixture.calls)})
    elif failure == "identity_after":
        monkeypatch.setattr(
            experiment,
            "parameter_identity",
            lambda *a: {
                "base": "f" * 64 if len(fixture.calls) > 86 else "b" * 64,
                "adapter": "c" * 64,
            },
        )
    else:
        monkeypatch.setattr(
            fixture.api, "append_row", lambda *a: (_ for _ in ()).throw(OSError("fsync"))
        )
    result = experiment.run(SimpleNamespace(plan="fixture", plan_sha256="a" * 64))
    assert result["native_exit_code"] == 1 and result["success"] is False
    assert (fixture.output / "report.json").is_file()
    assert all(value is False for value in result["claims"].values())
    if failure != "source_after":
        assert "score" not in fixture.calls
    if failure in {"dev_close", "identity_after"}:
        assert result["optimizer_steps"]["attempted"] == 800


@pytest.mark.parametrize(
    "verified,suppressed,exit_error",
    [(False, False, False), (1, False, False), (True, True, False), (True, False, True)],
)
def test_single_dev_prefix_session_unverified_suppressed_raising_exit_cannot_return(
    verified, suppressed, exit_error
):
    class Session:
        _active = False

        def __init__(self, root):
            self.verified = verified

        def __enter__(self):
            self._active = True
            return self

        def prime(self, value):
            assert len(value) == 1

        def __exit__(self, *args):
            self._active = False
            if exit_error:
                raise MemoryError("exit")
            return suppressed

    def actor(*args, **kwargs):
        if suppressed:
            raise ValueError("body")
        return SimpleNamespace(frames=(1,), identity_sha256="a" * 64)

    env = SimpleNamespace(
        actor=SimpleNamespace(NavigationActorInput=SimpleNamespace(from_supervision_sample=actor)),
        prefix=SimpleNamespace(
            process_actor=lambda *a: prefix(), validate_prefix_inputs=lambda *a, **k: None
        ),
        pilot=SimpleNamespace(tensor_record=tensor_record),
    )
    with pytest.raises((ValueError, MemoryError)):
        experiment.closed_prefix(
            sample("x", "a", 0),
            frozen_backend(),
            env,
            SimpleNamespace(ClosedNavigationSourceValidationSession=Session),
        )


def test_helper_sha_rejection_before_import(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    file = tmp_path / "dependency.py"
    file.write_bytes(b"raise AssertionError('must not import')")
    monkeypatch.setattr(experiment, "PINS", {"dependency.py": "a" * 64})
    monkeypatch.setattr(
        experiment.importlib, "import_module", lambda *a: pytest.fail("unadmitted import")
    )
    with pytest.raises(ValueError, match="SHA"):
        experiment.evaluation_api()


def test_guarded_import_is_stdlib_only():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; from scripts import experiment_onboard_navigation_action_readout; "
        "assert not any(n.split('.')[0] in {'torch','transformers','peft','PIL'} "
        "for n in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=root, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_cli_missing_plan_and_output_fields_no_model_load(monkeypatch):
    monkeypatch.setattr(experiment, "build_plan", lambda *a: pytest.fail("No plan admission"))
    monkeypatch.setattr(experiment, "run", lambda *a: pytest.fail("No model run"))
    assert experiment.main(["--emit-plan"]) == 1
    assert experiment.main(["--plan", "fixture"]) == 1
    assert (
        experiment.main(["--plan", "fixture", "--plan-sha256", "a" * 64, "--output-dir", "extra"])
        == 1
    )


@pytest.fixture
def plan_fixture(tmp_path, monkeypatch):
    """Actual plan/binding logic; inherited semantic gates are injected."""
    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    samples = old_samples()
    dev = tuple(sample(f"dev-{n}", "dev", n) for n in range(59))

    def write(name, value):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else experiment.canonical(value))
        return evaluation.record(path)

    for name in experiment.NEW_FILES:
        write(name, b"# source fixture only")
    code = write("dependency.py", b"# admitted synthetic dependency")
    monkeypatch.setattr(experiment, "PINS", {"dependency.py": code["sha256"]})
    weights = write("fixture-base.bin", b"fixture not model weights")
    old = write(
        "old.json",
        {
            "dataset_samples": write("rows.jsonl", b"[]"),
            "train_sample_ids": [s.sample_id for s in samples],
            "base": {"safetensors": weights},
        },
    )
    legacy = write("legacy.json", {"original_plan": old})
    prepared = write("prepared.json", {s.sample_id: {"fixture": True} for s in samples})
    recovery = write(
        "recovery.json", {"prepared_inputs": prepared, "expected_base_parameter_sha256": "b" * 64}
    )
    bridge = write("bridge.json", {"fixture": True})
    reference_predictions = write("old-eval/predictions.jsonl", b"[]")
    reference_report = write(
        "old-eval/report.json",
        {
            "native_exit_code": 0,
            "source_bindings_unchanged": True,
            "durable_prediction_count": 59,
            "predictions": reference_predictions,
        },
    )
    inherited = {
        "recovery_plan": recovery,
        "legacy_plan": legacy,
        "dataset_report": bridge,
        "source_files": [code],
        "runtime_sources": [],
        "sample_ids": [s.sample_id for s in dev],
        "output_dir": str(tmp_path / "old-eval"),
    }
    eval_plan = write("evaluation-plan.json", inherited)
    receipt = write(
        "receipt.json",
        {
            "contract": "multidetect.onboard-navigation-original59-actual-evaluation-evidence-v1",
            "actual_execution": {"actual_native_exit_code": 0, "processes_terminated": True},
            "bindings": [eval_plan, reference_report],
        },
    )
    terminal = write("terminal.json", {"fixture": True})
    monkeypatch.setattr(
        experiment,
        "BOUND",
        {
            "terminal": (terminal["path"], terminal["sha256"]),
            "actual59": (receipt["path"], receipt["sha256"]),
            "evaluation_plan": (eval_plan["path"], eval_plan["sha256"]),
        },
    )
    monkeypatch.setattr(experiment, "evaluation_api", lambda: evaluation)
    env = SimpleNamespace(
        old=SimpleNamespace(
            validate_prepared_inputs=lambda *a: None, BalancedTrainingBudget=lambda: None
        )
    )
    monkeypatch.setattr(evaluation, "apis", lambda: env)
    monkeypatch.setattr(
        evaluation,
        "verify_terminal",
        lambda *a: (
            {"weights": weights, "candidate86": weights},
            {"adapter_parameter_sha256_after": "c" * 64},
            {},
        ),
    )
    actors = {s.sample_id: {"actor_identity_sha256": "d" * 64} for s in dev}
    monkeypatch.setattr(evaluation, "admit_dataset", lambda *a: ((), dev, actors))
    monkeypatch.setattr(experiment, "load_samples", lambda *a: samples)
    monkeypatch.setattr(experiment, "runtime_readout_sources", lambda: [])
    output = tmp_path / "artifacts/evaluation/onboard-navigation-action-readout-plan-fixture"
    plan = experiment.build_plan(output)
    binding = write("plan.json", plan)
    return SimpleNamespace(
        root=tmp_path,
        write=write,
        plan=plan,
        binding=binding,
        output=output,
        code=code,
        eval_plan=eval_plan,
        receipt=receipt,
    )


def test_tiny_plan_emit_admit_deterministic_exact_newsources_and_roles(plan_fixture, capsys):
    f = plan_fixture
    assert f.plan["rule"] == experiment.RULE
    assert f.plan["rule"]["maximum_optimizer_attempts"] == 800
    assert f.plan["unsupported_development_actions"] == {"up": 1, "down": 1}
    assert f.plan["claims"]["independent_backbone_holdout"] is False
    assert f.plan["claims"]["pristine_base_or_warm_comparison"] is False
    assert experiment.build_plan(f.output) == f.plan
    admitted, _binding, _api = experiment.admit_plan(f.binding["path"], f.binding["sha256"])
    assert admitted == f.plan
    assert experiment.main(["--emit-plan", "--output-dir", str(f.output)]) == 0
    assert evaluation.strict_json(capsys.readouterr().out) == f.plan
    assert not f.output.exists()
    assert set(str(f.root / name) for name in experiment.NEW_FILES).issubset(
        row["path"] for row in f.plan["source_files"]
    )


@pytest.mark.parametrize(
    "change",
    [
        "budget",
        "fold",
        "dev23",
        "head_dimensions",
        "claims",
        "source",
        "wrongsha",
        "newsource_drift",
    ],
)
def test_plan_mutations_rejected_before_execute(plan_fixture, change):
    f = plan_fixture
    plan = evaluation.strict_json(experiment.canonical(f.plan))
    if change == "budget":
        plan["rule"]["maximum_optimizer_attempts"] = 801
    elif change == "fold":
        plan["folds"][0]["train_ids"].append(plan["folds"][0]["heldout_ids"][0])
    elif change == "dev23":
        plan["dev59_ids"].append("original23_train")
    elif change == "head_dimensions":
        plan["rule"]["feature_dim"] = 1024
    elif change == "claims":
        plan["claims"]["quality_claim"] = True
    elif change == "source":
        plan["source_files"] = []
    elif change == "newsource_drift":
        (f.root / experiment.NEW_FILES[0]).write_bytes(b"# source changed")
    if change not in {"wrongsha", "newsource_drift"}:
        bound = f.write("mutated-plan.json", plan)
    else:
        bound = f.binding
    with pytest.raises(ValueError):
        experiment.admit_plan(bound["path"], "a" * 64 if change == "wrongsha" else bound["sha256"])
    assert not f.output.exists()


def test_source_final_rule_no_best_scope_and_createonly_plan(plan_fixture):
    f = plan_fixture
    assert f.plan["rule"]["checkpoint_rule"] == "final_update_only_no_best_no_retry"
    f.output.mkdir(parents=True)
    (f.output / "retained").write_bytes(b"retain")
    with pytest.raises(FileExistsError):
        experiment.admit_plan(f.binding["path"], f.binding["sha256"])
    assert (f.output / "retained").read_bytes() == b"retain"


def test_metrics_keep_vertical_denominator_and_present_macro():
    rows = []
    for category in ("forward", "up", "down", "stop"):
        gold = evaluation.gold_mapping(sample(category, "x", 0, category))
        forward = evaluation.gold_mapping(sample("f", "x", 0))
        score = evaluation.score_prediction(experiment.canonical(forward).decode(), gold, inference)
        score["head_label_supported"] = category in {"forward", "stop"}
        score.update(
            raw_selected_class="forward",
            raw_selected_class_correct=category == "forward",
            selected_scale_rejected=False,
        )
        rows.append({"score": score})
    summary = experiment.summarize_scores(rows)
    assert summary["rows"] == 4 and summary["unsupported"] == 2
    assert summary["action_correct"] == 1 and summary["macro_action_recall_present_classes"] == 0.25
    assert summary["per_class"]["up"]["action_recall"] == 0
    assert summary["per_class"]["down"]["action_recall"] == 0


@pytest.mark.parametrize("drift", [None, "pixels", "ids", "actor", "missing_four"])
def test_retained_json_reference_rederived_budget_and_raw_not_faked(tmp_path, monkeypatch, drift):
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    samples = {"a": sample("a", "episode", 0), "b": sample("b", "episode", 1)}
    raw = experiment.canonical(evaluation.gold_mapping(samples["a"])).decode()
    generated = {
        "raw_output": raw,
        "output_token_ids": [1, 2],
        "generated_tokens": 2,
        "elapsed_s": 31.0,
        "token_cap_reached": False,
        "peak_allocated_bytes": 0,
        "peak_reserved_bytes": 0,
    }
    records = {k: tensor_record(v) for k, v in prefix().items()}
    first = [
        {
            "sample_id": "a",
            "generated": generated,
            "actual_prefix_tensors": records,
            "actor_identity_sha256": "a" * 64,
        }
    ]
    second = [
        {
            "sample_id": "b",
            "generated": generated | {"elapsed_s": 2.0},
            "prefix": {"tensors": records},
            "actor_identity_sha256": "a" * 64,
        }
    ]
    (tmp_path / "old.json").write_bytes(experiment.canonical(first))
    (tmp_path / "dev.jsonl").write_bytes(experiment.canonical(second[0]) + b"\n")
    plan = {
        "generative_reference86": evaluation.record(tmp_path / "old.json"),
        "generative_reference59": evaluation.record(tmp_path / "dev.jsonl"),
        "old86_ids": ["a"],
        "dev59_ids": ["b"],
    }
    rows = [
        {
            "sample_id": sid,
            "condition": condition,
            "score": evaluation.score_prediction(
                raw, evaluation.gold_mapping(samples[sid]), inference
            ),
        }
        for sid, condition in (("a", "loeo"), ("b", "locked_final86_dev59"))
    ]
    observations = {
        sid: {
            "prefix_tensors": {k: v.copy() for k, v in records.items()},
            "actor_identity_sha256": "a" * 64,
        }
        for sid in samples
    }
    if drift in {"pixels", "ids"}:
        observations["a"]["prefix_tensors"]["pixel_values" if drift == "pixels" else "input_ids"][
            "sha256"
        ] = "b" * 64
    elif drift == "actor":
        observations["b"]["actor_identity_sha256"] = "b" * 64
    elif drift == "missing_four":
        del observations["b"]["prefix_tensors"]["image_grid_thw"]
    if drift is not None:
        with pytest.raises(ValueError, match="Actor or exact four"):
            experiment.compare_generative_reference(
                plan, rows, samples, evaluation, SimpleNamespace(inference=inference), observations
            )
        return
    result = experiment.compare_generative_reference(
        plan, rows, samples, evaluation, SimpleNamespace(inference=inference), observations
    )
    assert result["old86"]["paired"]["action_raw"] == {"both_correct": 1}
    assert result["old86"]["paired"]["action_reference_budget_eligible"] == {"head_only": 1}
    assert result["dev59"]["paired"]["action_reference_budget_eligible"] == {"both_correct": 1}
    assert result["old86"]["recorded_generation_elapsed_mean_s"] == 31
    assert result["old86"]["no_generation_budget_assigned_to_readout"] is True
    assert "historical_different_run" in result["old86"]["timing_scope"]
    assert result["dev59"]["same_actor_and_four_prefix_tensors_verified"] is True


def test_actual_caller_reloaded_raw_output_change_rejects_before_dev_or_gold(
    orchestration, monkeypatch
):
    original = experiment.save_reload_head

    def altered(*args):
        clone, files = original(*args)
        with torch.no_grad():
            clone.linear.bias[0].add_(1.0)
        return clone, files

    monkeypatch.setattr(experiment, "save_reload_head", altered)
    f = orchestration
    report = experiment.run(SimpleNamespace(plan="fixture", plan_sha256="a" * 64))
    assert report["native_exit_code"] == 1
    assert "six-output" in report["error"]["message"]
    assert report["optimizer_steps"]["attempted"] == 200
    assert report["head_locked_before_dev59"] is False
    assert "dev_extract" not in f.calls and "score" not in f.calls
    assert report["raw_prediction_count"] == 0


def test_loader_cleanup_failure_preserves_actual_loaded_fact(orchestration, monkeypatch):
    f = orchestration
    env = f.api.apis()
    monkeypatch.setattr(
        env.recovery,
        "check_loader_cleanup",
        lambda *a: (_ for _ in ()).throw(ValueError("loader cleanup")),
    )
    report = experiment.run(SimpleNamespace(plan="fixture", plan_sha256="a" * 64))
    assert report["native_exit_code"] == 1 and report["success"] is False
    assert report["model_loaded"] is True and report["model_loading_attempted"] is True
    assert report["feature_count"] == 0 and report["optimizer_steps"]["attempted"] == 0


def test_plan_output_python_createonly_bytes_binding_preserves_large_integer(plan_fixture, capsys):
    f = plan_fixture
    path = f.root / "configs/training/onboard-navigation-action-readout-fixture.json"
    f.plan["fixture_uint64"] = 18446744073709551614
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(experiment, "build_plan", lambda *a: f.plan)
        assert (
            experiment.main(
                ["--emit-plan", "--output-dir", str(f.output), "--plan-output", str(path)]
            )
            == 0
        )
    binding = evaluation.strict_json(capsys.readouterr().out)
    assert binding == evaluation.record(path)
    assert path.read_bytes() == experiment.canonical(f.plan) + b"\n"
    assert evaluation.strict_json(path.read_bytes())["fixture_uint64"] == 18446744073709551614
    retained = path.read_bytes()
    assert (
        experiment.main(["--emit-plan", "--output-dir", str(f.output), "--plan-output", str(path)])
        == 1
    )
    assert path.read_bytes() == retained and not f.output.exists()


@pytest.mark.parametrize(
    "relative",
    [
        "old-plan.json",
        "configs/training/existing.json",
        "configs/training/onboard-navigation-action-readout-fixture.txt",
        "../configs/training/onboard-navigation-action-readout-fixture.json",
    ],
)
def test_plan_output_requires_new_specific_repository_plan_scope(plan_fixture, relative):
    f = plan_fixture
    assert (
        experiment.main(["--emit-plan", "--output-dir", str(f.output), "--plan-output", relative])
        == 1
    )


def test_execute_rejects_plan_output_before_run(monkeypatch):
    monkeypatch.setattr(experiment, "run", lambda *a: pytest.fail("No experiment execution"))
    assert (
        experiment.main(
            [
                "--plan",
                "fixture",
                "--plan-sha256",
                "a" * 64,
                "--plan-output",
                "configs/training/onboard-navigation-action-readout-x.json",
            ]
        )
        == 1
    )


@pytest.mark.parametrize("drift", [None, "executable", "version", "missing_package"])
def test_actual_runtime_executable_five_versions_gate_before_import(tmp_path, monkeypatch, drift):
    versions = {
        name: "fixture-1" for name in ("torch", "transformers", "peft", "accelerate", "safetensors")
    }
    runtime = {"python_executable": sys.executable, "package_versions": versions.copy()}
    if drift == "executable":
        runtime["python_executable"] = str(tmp_path / "other-python.exe")
    elif drift == "version":
        runtime["package_versions"]["peft"] = "fixture-other"
    elif drift == "missing_package":
        del runtime["package_versions"]["accelerate"]
    monkeypatch.setattr(experiment.importlib.metadata, "version", lambda name: versions[name])
    monkeypatch.setattr(
        experiment.importlib,
        "import_module",
        lambda *a: pytest.fail("Runtime reject must precede import"),
    )
    api = SimpleNamespace(read_bound=lambda value: runtime if value == "runtime" else pytest.fail())
    plan = {"evaluation_plan": {"runtime_reference": "runtime"}}
    if drift:
        with pytest.raises(ValueError, match="runtime|distributions|distribution"):
            experiment.admit_runtime(plan, api)
    else:
        result = experiment.admit_runtime(plan, api)
        assert result["package_versions"] == versions
        assert result["python_executable"] == str(Path(sys.executable).resolve())


@pytest.mark.parametrize("drift", [None, "module_path", "bytes", "missing", "duplicate"])
def test_actual_imported_runtime_files_match_declared_sha_before_after(
    tmp_path, monkeypatch, drift
):
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    names = {
        "torch": "torch/__init__.py",
        "peft": "peft/__init__.py",
        "torch.optim.sgd": "torch/optim/sgd.py",
        "safetensors.torch": "safetensors/torch.py",
    }
    modules, bindings = {}, []
    for name, relative in names.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"# CPU stand-in runtime entrypoint, not imported")
        modules[name] = SimpleNamespace(__file__=str(path))
        bindings.append(evaluation.record(path))
    plan = {"runtime_sources": bindings}
    imports = []

    def import_module(name):
        imports.append(name)
        return modules[name]

    monkeypatch.setattr(experiment.importlib, "import_module", import_module)
    admitted = experiment.verify_runtime_modules(plan, evaluation)
    assert set(admitted) == set(names) and imports == list(names)
    if drift == "module_path":
        modules["torch.optim.sgd"].__file__ = str(tmp_path / "foreign-sgd.py")
    elif drift == "bytes":
        Path(modules["safetensors.torch"].__file__).write_bytes(b"# drift after first admission")
    elif drift == "missing":
        bindings.pop()
    elif drift == "duplicate":
        bindings.append(bindings[0].copy())
    if drift:
        with pytest.raises(ValueError, match="module path|bytes changed|binding required"):
            experiment.verify_runtime_modules(plan, evaluation)
    else:
        assert experiment.verify_runtime_modules(plan, evaluation) == admitted


def test_runtime_failure_prevents_head_import_and_model_load(orchestration, monkeypatch):
    monkeypatch.setattr(
        experiment,
        "admit_runtime",
        lambda *a: (_ for _ in ()).throw(ValueError("runtime executable")),
    )
    monkeypatch.setattr(
        experiment.importlib,
        "import_module",
        lambda *a: pytest.fail("No head/processor/model imports"),
    )
    report = experiment.run(SimpleNamespace(plan="fixture", plan_sha256="a" * 64))
    assert report["native_exit_code"] == 1 and report["model_loaded"] is False
    assert "model_loading_attempted" not in report
    assert report["feature_count"] == 0 and report["optimizer_steps"]["attempted"] == 0


def test_raw_classifier_correct_with_rejected_scale_is_not_accepted_action_or_joint():
    row = {"raw": {"selected_class": "forward"}, "selected_scale_rejected": True}
    gold = sample("f", "train", 0)
    score = experiment.score_readout_row(
        row, gold, evaluation, SimpleNamespace(inference=inference)
    )
    summary = experiment.summarize_scores([{"score": score}])
    assert score["raw_selected_class_correct"] is True
    assert score["action_class_correct"] is False and score["metric_correct"] is None
    assert summary["action_correct"] == 0 and summary["joint_correct"] == 0
    assert summary["raw_selected_class_accuracy"] == 1
    assert summary["raw_class_confusion"] == {"forward": {"forward": 1}}
    assert summary["scale_rejected_count"] == 1


def test_train_derived_forward_bias_reference_54_of_59_joint_macro_point2_no_fake_tokens():
    samples = {
        str(n): sample(
            str(n),
            "development",
            n,
            "forward"
            if n < 54
            else "stop"
            if n < 56
            else "yaw_right"
            if n == 56
            else "up"
            if n == 57
            else "down",
        )
        for n in range(59)
    }
    rows = [
        {
            "sample_id": sid,
            "score": {
                "gold_class": evaluation.primitive_class(
                    inference.parse_navigation_target(
                        experiment.canonical(evaluation.gold_mapping(s))
                    )
                )
            },
        }
        for sid, s in samples.items()
    ]
    target = experiment.train_forward_reference(old_samples(), evaluation)
    assert target["translation_m"] == [3.0, 0.0, 0.0]
    reference = experiment.always_forward_reference(
        rows, target, samples, evaluation, SimpleNamespace(inference=inference)
    )
    assert reference["rows"] == 59 and reference["action_correct"] == 54
    assert reference["joint_correct"] == 54
    assert reference["macro_action_recall_present_classes"] == 0.2
    assert reference["gold_class_counts"]["up"] == reference["gold_class_counts"]["down"] == 1
    assert "no_dev_fit_tokens_or_timing" in reference["scope"]
    assert not {"generated_tokens", "elapsed_s", "output_token_ids"} & reference.keys()
    other = sample("other", "training", 0)
    other.planned_action.translation_m = (4.0, 0.0, 0.0)
    assert experiment.train_forward_reference(old_samples() + (other,), evaluation) is None

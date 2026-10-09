from __future__ import annotations

import hashlib
import io
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts import train_onboard_vlm_navigation_pilot as pilot


def target(kind="translate", translation=None, yaw=None):
    return {
        "schema_version": 1,
        "contract": pilot.navigation.TARGET_CONTRACT,
        "kind": kind,
        "translation_frame": "body_frd",
        "yaw_convention": "clockwise_right_positive",
        "translation_m": [3.0, 0.0, 0.0]
        if translation is None and kind == "translate"
        else translation,
        "yaw_delta_rad": yaw,
    }


def bound(path, size=1, sha="a" * 64):
    return {"path": str(path), "bytes": size, "sha256": sha}


def plan(tmp_path):
    snapshot = tmp_path / pilot.core.REVISION
    return {
        "schema_version": 1,
        "contract": pilot.PLAN_CONTRACT,
        "artifact_root": str(tmp_path),
        "dataset_report": bound(tmp_path / "report.json"),
        "dataset_samples": bound(tmp_path / "samples.jsonl"),
        "review_receipt": bound(tmp_path / "review.json"),
        "source_files": [bound(tmp_path / p) for p in pilot.SOURCE_PATHS],
        "base": {
            "model_id": pilot.core.MODEL_ID,
            "revision": pilot.core.REVISION,
            "snapshot": str(snapshot),
            "safetensors": bound(
                snapshot / "model.safetensors", pilot.BASE_BYTES, pilot.BASE_SHA256
            ),
            "processor_files": [bound(snapshot / "config.json")],
        },
        "optimization": asdict(pilot.NavigationTrainingBudget()),
        "expected_samples": 86,
        "train_sample_ids": [f"sample-{i}" for i in range(86)],
        "diagnostic_probes": [{"sample_id": "sample-0", "action_class": "forward"}],
        "claims": dict(pilot.CLAIMS),
    }


def test_bound_plan_exact_sha_and_schema(tmp_path):
    document = plan(tmp_path)
    pilot.validate_plan_shape(document)
    path = tmp_path / "plan.json"
    raw = pilot.core.canonical_bytes(document)
    path.write_bytes(raw)
    assert pilot.load_plan(path, hashlib.sha256(raw).hexdigest()) == document
    path.write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="Plan SHA"):
        pilot.load_plan(path, hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize(
    "mutation", ["quality", "source", "base", "steps", "duplicate", "extra", "probe", "rank"]
)
def test_plan_rejects_authority_missing_binding_model_budget_and_probes(tmp_path, mutation):
    value = plan(tmp_path)
    if mutation == "quality":
        value["claims"]["quality_claim"] = True
    elif mutation == "source":
        value["source_files"].pop()
    elif mutation == "base":
        value["base"]["safetensors"]["sha256"] = "b" * 64
    elif mutation == "steps":
        value["expected_samples"] = 85
    elif mutation == "duplicate":
        value["train_sample_ids"][1] = value["train_sample_ids"][0]
    elif mutation == "extra":
        value["default_adapter"] = "unqualified-existing-grounding"
    elif mutation == "probe":
        value["diagnostic_probes"][0]["sample_id"] = "validation-sample"
    else:
        value["optimization"]["rank"] = 8
    with pytest.raises(ValueError):
        pilot.validate_plan_shape(value)


@pytest.mark.parametrize(
    "name,value",
    [
        ("learning_rate", 1e-4),
        ("epochs", 2),
        ("max_pixels_per_image", 200704),
        ("max_optimizer_steps", 101),
        ("gradient_checkpointing", False),
        ("seed", 1),
        ("max_cuda_reserved_bytes", 9000000000),
    ],
)
def test_frozen_budget_no_automatic_fallback(name, value):
    with pytest.raises(ValueError):
        replace(pilot.NavigationTrainingBudget(), **{name: value}).validate()


def test_full_snapshot_file_set_no_unbound_processor_config(tmp_path, monkeypatch):
    value = plan(tmp_path)
    snapshot = Path(value["base"]["snapshot"])
    snapshot.mkdir()
    for name in ("model.safetensors", "config.json", "tokenizer.json"):
        (snapshot / name).write_bytes(b"fixture")
    monkeypatch.setattr(pilot, "verify_binding", lambda v, root: Path(v["path"]))
    with pytest.raises(ValueError, match="full file set"):
        pilot.verify_plan_sources(value)
    value["base"]["processor_files"].append(bound(snapshot / "tokenizer.json"))
    pilot.verify_plan_sources(value)
    value["base"]["processor_files"].append(bound(snapshot / "tokenizer.json"))
    with pytest.raises(ValueError, match="full file set"):
        pilot.verify_plan_sources(value)


def test_binding_actual_bytes_change_and_escape_rejected(tmp_path):
    path = tmp_path / "original"
    path.write_bytes(b"original")
    spec = bound(path, 8, hashlib.sha256(b"original").hexdigest())
    assert pilot.verify_binding(spec, tmp_path) == path
    path.write_bytes(b"modified")
    with pytest.raises(ValueError, match="SHA changed"):
        pilot.verify_binding(spec, tmp_path)
    with pytest.raises(ValueError, match="escaped"):
        pilot.verify_binding(
            bound(Path(pilot.__file__), Path(pilot.__file__).stat().st_size, "a" * 64), tmp_path
        )


class FakeProcessor:
    def __init__(self, torch, mutation=None):
        self.torch, self.mutation, self.calls = torch, mutation, []
        self.image_processor = SimpleNamespace(patch_size=14)
        self.tokenizer = SimpleNamespace(convert_tokens_to_ids=lambda _: 99)

    def apply_chat_template(self, messages, **kwargs):
        limits = kwargs.pop("images_kwargs")
        self.calls.append(dict(limits))
        limits.clear()  # Real official processor consumes its nested mapping.
        images = sum(item["type"] == "image" for m in messages for item in m["content"])
        full = not kwargs["add_generation_prompt"]
        ids = [11, 99, 12, 13] + ([21, 22] if full else [])
        grids = [[1, 4, 4] for _ in range(images)]
        if self.mutation == "oversize":
            grids[0] = [1, 32, 32]
        if self.mutation == "future":
            grids[0][0] = 2
        pixels = self.torch.zeros((images * 16, 12))
        if self.mutation == "mismatch" and full:
            pixels[0, 0] = 1
        if self.mutation == "nan":
            pixels[0, 0] = float("nan")
        if self.mutation == "patches":
            pixels = pixels[:-1]
        if self.mutation == "prefix" and full:
            ids[0] = 123
        tensors = {
            "input_ids": self.torch.tensor([ids]),
            "attention_mask": self.torch.ones((1, len(ids)), dtype=self.torch.long),
            "image_grid_thw": self.torch.tensor(grids),
            "pixel_values": pixels,
        }
        if self.mutation == "extra":
            tensors["video_grid_thw"] = self.torch.tensor([[1, 4, 4]])
        return tensors


def encoder(torch, mutation=None):
    value = object.__new__(pilot.NavigationBackend)
    value.torch, value.budget = torch, pilot.NavigationTrainingBudget()
    value.processor = FakeProcessor(torch, mutation)
    return value


def messages(count):
    return [
        {"role": "system", "content": [{"type": "text", "text": "frozen rules"}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "VERBATIM_SOURCE_PROMPT"},
                *[{"type": "image", "image": Image.new("RGB", (100, 80))} for _ in range(count)],
            ],
        },
    ]


@pytest.mark.parametrize("count", [1, 2])
def test_real_cpu_tensor_encode_fresh_image_kwargs_mask_grid_identity(count):
    torch = pytest.importorskip("torch")
    backend = encoder(torch)
    value = backend.encode_messages(messages(count), pilot.core.canonical_bytes(target()).decode())
    assert backend.processor.calls == [{"min_pixels": 1024, "max_pixels": 50176}] * 2
    assert len(value["actual_image_grid_thw"]) == count
    assert value["actual_image_pixels"] == [3136] * count
    assert value["labels"].tolist() == [[-100, -100, -100, -100, 21, 22]]
    assert value["prefix_tensors"]["pixel_values"] == value["input_tensors"]["pixel_values"]
    assert value["prefix_tensors"]["image_grid_thw"] == value["input_tensors"]["image_grid_thw"]


@pytest.mark.parametrize(
    "mutation", ["oversize", "future", "mismatch", "nan", "patches", "prefix", "extra"]
)
def test_processor_budget_and_actual_tensor_disagreement_rejected(mutation):
    torch = pytest.importorskip("torch")
    with pytest.raises(ValueError):
        encoder(torch, mutation).encode_messages(messages(2), "{}")


@pytest.mark.parametrize("count", [0, 3])
def test_no_missing_or_silent_dropped_real_frames(count):
    torch = pytest.importorskip("torch")
    with pytest.raises(ValueError, match="past real RGB"):
        encoder(torch).encode_messages(messages(count), "{}")


@pytest.mark.parametrize(
    "value",
    [
        target(translation=[0, 0, 0]),
        target(translation=[501, 0, 0]),
        target("turn_yaw", yaw=0),
        target("turn_yaw", yaw=4.0),
        target("stop", translation=[1, 0, 0]),
        target(translation=[True, 0, 0]),
    ],
)
def test_nonzero_bounds_and_stop_do_not_grant_landing(value):
    with pytest.raises(ValueError):
        pilot.validate_target(value)
    assert not pilot.diagnose(json.dumps(value), target())["strict_json_valid"]


def test_strict_duplicate_and_nonfinite_json_diagnostics_not_accuracy():
    for raw in ('{"kind":"stop","kind":"translate"}', '{"yaw_delta_rad":NaN}', "```json\n{}\n```"):
        assert not pilot.diagnose(raw, target())["strict_json_valid"]
    good = pilot.diagnose(json.dumps(target()), target())
    assert good["strict_json_valid"] and good["action_class_correct"] and good["metric_correct"]
    assert good["resubstitution_only"] and not good["quality_claim"]
    incorrect_metric = pilot.diagnose(json.dumps(target(translation=[6, 0, 0])), target())
    assert incorrect_metric["action_class_correct"] and not incorrect_metric["metric_correct"]
    unknown = target()
    unknown["translation_m"] = None
    result = pilot.diagnose(json.dumps(unknown), target())
    assert (
        result["strict_json_valid"]
        and not result["action_class_correct"]
        and not result["metric_correct"]
    )
    unknown_turn = pilot.diagnose(json.dumps(target("turn_yaw")), target("turn_yaw", yaw=-0.5))
    assert unknown_turn["strict_json_valid"]
    assert unknown_turn["predicted_action_class"] == "unknown_turn"
    assert not unknown_turn["metric_correct"] and "rejection" not in unknown_turn


def test_pre_optimizer_review_revocation_blocks_before_training(tmp_path, monkeypatch):
    value = plan(tmp_path)
    events = []

    def revoked(_):
        events.append("sources/review-rechecked")
        raise ValueError("review receipt revoked")

    monkeypatch.setattr(pilot, "verify_plan_sources", revoked)
    monkeypatch.setattr(pilot, "train_epoch", lambda *a: events.append("optimizer-created"))
    with pytest.raises(ValueError, match="revoked"):
        pilot.pre_optimizer_admission(value, tmp_path / "plan.json", "a" * 64, ())
    assert events == ["sources/review-rechecked"]


class FakeCuda:
    peak = 4096

    def reset_peak_memory_stats(self):
        pass

    def synchronize(self):
        pass

    def max_memory_allocated(self):
        return self.peak

    def max_memory_reserved(self):
        return self.peak

    def mem_get_info(self):
        return (6 * 1024**3, 8 * 1024**3)

    def memory_reserved(self):
        return 4096


def cpu_training_backend(torch):
    model = torch.nn.Linear(1, 1, bias=False)
    with torch.no_grad():
        model.weight.fill_(1)
    facade = SimpleNamespace(
        nn=torch.nn, optim=torch.optim, cuda=FakeCuda(), isfinite=torch.isfinite
    )
    return SimpleNamespace(torch=facade, model=model, loss=lambda _: model.weight.square().sum())


def training_report():
    return {
        "native_exit_code": 1,
        "optimizer_steps_attempted": 0,
        "optimizer_steps_returned": 0,
        "optimizer_steps_synchronized": 0,
        "optimizer_steps_completed": 0,
        "forward_backward_performed": False,
    }


def test_cpu_complete_epoch_journals_each_attempt_return_sync_completed():
    torch = pytest.importorskip("torch")
    backend = cpu_training_backend(torch)
    samples = [SimpleNamespace(sample_id=f"sample-{i}") for i in range(86)]
    report, stream = training_report(), io.StringIO()
    before = backend.model.weight.detach().clone()
    pilot.train_epoch(
        backend,
        samples,
        {s.sample_id: {} for s in samples},
        pilot.NavigationTrainingBudget(),
        report,
        stream,
    )
    assert report["optimizer_steps_completed"] == 86
    assert not torch.equal(before, backend.model.weight)
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(rows) == 4 * 86
    for i in range(86):
        assert [r["stage"] for r in rows[i * 4 : (i + 1) * 4]] == [
            "optimizer_step_attempted",
            "optimizer_step_returned",
            "optimizer_step_synchronized",
            "optimizer_step_completed",
        ]


def test_cpu_optimizer_mid_update_exception_keeps_attempt_uncertain():
    torch = pytest.importorskip("torch")
    backend = cpu_training_backend(torch)
    underlying = torch.optim.AdamW

    def optimizer(parameters, **kwargs):
        original = underlying(parameters, **kwargs)
        original_step = original.step

        def partial_step():
            original_step()
            raise RuntimeError("fake step raised after a parameter update")

        original.step = partial_step
        return original

    backend.torch.optim = SimpleNamespace(AdamW=optimizer)
    samples = [SimpleNamespace(sample_id=f"sample-{i}") for i in range(86)]
    report, stream = training_report(), io.StringIO()
    before = backend.model.weight.detach().clone()
    with pytest.raises(RuntimeError) as failure:
        pilot.train_epoch(
            backend,
            samples,
            {s.sample_id: {} for s in samples},
            pilot.NavigationTrainingBudget(),
            report,
            stream,
        )
    pilot.record_failure(report, failure.value)
    assert report["optimizer_steps_attempted"] == 1 and report["optimizer_steps_completed"] == 0
    assert report["optimizer_update_uncertain"] and report["adapter_acceptance_uncertain"]
    assert not torch.equal(before, backend.model.weight)
    assert [json.loads(v)["stage"] for v in stream.getvalue().splitlines()] == [
        "optimizer_step_attempted"
    ]


def test_memory_cap_and_device_usage_are_separate():
    cuda = FakeCuda()
    backend = SimpleNamespace(torch=SimpleNamespace(cuda=cuda))
    result = pilot.check_memory(backend, pilot.NavigationTrainingBudget())
    assert result["device_total_bytes"] == 8 * 1024**3
    assert result["other_usage_is_estimate_not_process_attribution"]
    cuda.peak = 8 * 1024**3
    with pytest.raises(ValueError, match="CUDA peak"):
        pilot.check_memory(backend, pilot.NavigationTrainingBudget())


def test_failure_report_create_only_and_no_backend_on_changed_plan(tmp_path):
    output = tmp_path / "attempt"
    args = SimpleNamespace(plan=tmp_path / "missing.json", plan_sha256="a" * 64, output_dir=output)
    called = []
    assert pilot.run(args, backend_factory=lambda *a: called.append(a)) == 1
    assert not called
    value = json.loads((output / "report.json").read_bytes())
    assert value["optimizer_steps_attempted"] == 0 and value["optimizer_performed"] is False
    assert not value["quality_claim"] and not value["candidate_promoted"]
    with pytest.raises(FileExistsError):
        pilot.run(args)


@pytest.mark.parametrize(
    "counts,uncertain",
    [
        ((0, 0, 0, 0), False),
        ((1, 0, 0, 0), True),
        ((1, 1, 0, 0), True),
        ((1, 1, 1, 0), True),
        ((86, 86, 86, 86), False),
    ],
)
def test_failed_step_counter_disagreement_stays_uncertain(counts, uncertain):
    report = training_report()
    for key, value in zip(
        (
            "optimizer_steps_attempted",
            "optimizer_steps_returned",
            "optimizer_steps_synchronized",
            "optimizer_steps_completed",
        ),
        counts,
        strict=True,
    ):
        report[key] = value
    pilot.record_failure(report, RuntimeError("injected phase failure"))
    assert report["optimizer_update_uncertain"] is uncertain
    assert report["adapter_acceptance_uncertain"] is (counts[0] > 0)


def test_pilot_cuda_out_of_memory_context_retained_without_fallback():
    report = training_report()
    failure = RuntimeError("CUDA out of memory before first optimizer")
    failure.pre_load_cuda_memory = {"device_free_bytes": 123, "external_processes_stopped": False}
    pilot.record_failure(report, failure)
    assert report["pre_load_cuda_memory"] == failure.pre_load_cuda_memory
    assert report["native_exit_code"] == 1 and report["optimizer_steps_attempted"] == 0


@pytest.mark.parametrize(
    "receipt",
    [
        [],
        {},
        {
            "schema_version": True,
            "contract": pilot.REVIEW_CONTRACT,
            "dataset_report_sha256": "a" * 64,
            "dataset_samples_sha256": "a" * 64,
            "allowed_training_use": "planned_navigation_train_only_resubstitution_pilot",
            "optimizer_allowed": True,
            "independent_quality_evidence": False,
        },
    ],
)
def test_review_receipt_strict_type_no_early_backend_or_model(tmp_path, monkeypatch, receipt):
    monkeypatch.setattr(pilot, "read_bound_json", lambda *a: receipt)
    with pytest.raises(ValueError):
        pilot.admit_dataset(plan(tmp_path))

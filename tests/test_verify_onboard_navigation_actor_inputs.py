"""CPU fixtures distinguish prefix bytes, admission, output and claim boundaries."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_navigation_actor import source_sample as source_sample

from multidetect.autonomy import onboard_navigation_actor as actor_module
from multidetect.autonomy.onboard_navigation_supervision import (
    NavigationSourceValidationSession,
    NavigationSupervisionBudget,
)
from multidetect.autonomy.serialization import AutonomyContractError
from scripts import run_onboard_navigation_pilot_supervised as supervisor
from scripts import train_onboard_vlm_navigation_pilot as pilot
from scripts import verify_onboard_navigation_actor_inputs as verifier


@pytest.fixture(scope="module")
def torch_cpu():
    torch = importlib.import_module("torch")
    assert not torch.cuda.is_initialized()
    return torch


class CPUFixtureImageProcessor:
    patch_size = 4
    temporal_patch_size = 2


class CPUFixtureTokenizer:
    pass


class CPUFixtureProcessor:
    """A deliberately consuming test processor; never official acceptance."""

    def __init__(self, torch, *, mutate_second=False):
        self.torch = torch
        self.image_processor = CPUFixtureImageProcessor()
        self.tokenizer = CPUFixtureTokenizer()
        self.calls = []
        self.images = []
        self.kwargs_references = []
        self.mutate_second = mutate_second

    def apply_chat_template(self, messages, **kwargs):
        assert kwargs["add_generation_prompt"] is True
        assert kwargs["tokenize"] is True and kwargs["return_dict"] is True
        assert kwargs["return_tensors"] == "pt"
        options = kwargs["images_kwargs"]
        assert options == {"min_pixels": 1024, "max_pixels": 50176}
        self.kwargs_references.append(options)
        options.clear()  # Reusing one nested mapping would break the second call.
        texts, images = [], []
        for message in messages:
            for item in message["content"]:
                if item["type"] == "text":
                    texts.append(item["text"])
                else:
                    image = item["image"]
                    assert image.mode == "RGB" and not image.info
                    images.append(image)
                    texts.append(hashlib.sha256(image.tobytes()).hexdigest())
        self.calls.append(texts)
        self.images.extend(images)
        fingerprint = int(hashlib.sha256("\n".join(texts).encode()).hexdigest()[:12], 16)
        result = _inputs(self.torch, image_count=len(images), token=fingerprint)
        if self.mutate_second and len(self.calls) == 2:
            result["pixel_values"][0, 0] += 1.0
        # The official processor is allowed to consume message mappings too.
        for message in messages:
            message["content"].clear()
        return result


def _inputs(torch, *, image_count=1, token=123):
    return {
        "input_ids": torch.tensor([[13, token, 29]], dtype=torch.int64),
        "attention_mask": torch.ones((1, 3), dtype=torch.int64),
        "pixel_values": torch.full((64 * image_count, 96), token % 251 / 251.0),
        "image_grid_thw": torch.tensor([[1, 8, 8]] * image_count, dtype=torch.int64),
    }


def _validate(inputs, torch, *, processor=None, image_count=1, **limits):
    return verifier.validate_prefix_inputs(
        inputs,
        processor=processor or CPUFixtureProcessor(torch),
        image_count=image_count,
        torch_module=torch,
        tensor_record=pilot.tensor_record,
        **limits,
    )


@pytest.mark.parametrize("image_count", [1, 2])
def test_exact_prefix_tensor_bytes_and_fresh_consumable_messages(
    source_sample, tmp_path, torch_cpu, image_count
):
    sample = replace(source_sample, frames=source_sample.frames[-image_count:])
    processor = CPUFixtureProcessor(torch_cpu)
    budget = NavigationSupervisionBudget()
    with NavigationSourceValidationSession(tmp_path, budget=budget) as session:
        session.prime((sample,))
        result = verifier.verify_sample_pair(
            sample,
            tmp_path,
            session,
            processor,
            actor_module=actor_module,
            pilot=pilot,
            budget=budget,
            torch_module=torch_cpu,
        )
    assert session.verified
    assert result["training_prefix"] == result["observable_prefix"]
    assert set(result["training_prefix"]["tensors"]) == verifier.FIELDS
    assert result["training_prefix"]["actual_image_pixels"] == [1024] * image_count
    assert result["retained_history_valid"] and result["all_four_tensor_bytes_equal"]
    assert not result["labels_pose_future_ids_entered_model_messages"]
    assert processor.calls[0] == processor.calls[1]
    assert processor.kwargs_references[0] is not processor.kwargs_references[1]
    assert len(processor.images) == 2 * image_count
    for image in processor.images:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))
    model_text = "\n".join(processor.calls[0])
    for forbidden in (sample.sample_id, "secret-gold-goal", "opaque-frame-", "imageid"):
        assert forbidden not in model_text
    assert not torch_cpu.cuda.is_initialized()


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_field",
        "missing_field",
        "non_tensor",
        "meta_device",
        "autograd",
        "ids_float",
        "ids_negative",
        "batch_two",
        "empty_tokens",
        "too_many_tokens",
        "mask_float",
        "mask_shape",
        "mask_nonbinary",
        "mask_all_zero",
        "grid_float",
        "grid_missing_image",
        "grid_extra_image",
        "grid_temporal",
        "grid_zero",
        "pixels_below_min",
        "pixels_above_max",
        "patch_bool",
        "temporal_zero",
        "pixels_float64",
        "pixels_shape",
        "pixels_nonfinite",
    ],
)
def test_processor_tensor_admission_rejects_invalid_actual_outputs(torch_cpu, mutation):
    torch, processor = torch_cpu, CPUFixtureProcessor(torch_cpu)
    value = _inputs(torch)
    if mutation == "extra_field":
        value["untrusted_position"] = torch.zeros(1)
    elif mutation == "missing_field":
        del value["attention_mask"]
    elif mutation == "non_tensor":
        value["input_ids"] = [[13, 123, 29]]
    elif mutation == "meta_device":
        value["pixel_values"] = torch.empty((64, 96), device="meta")
    elif mutation == "autograd":
        value["pixel_values"].requires_grad_(True)
    elif mutation == "ids_float":
        value["input_ids"] = value["input_ids"].float()
    elif mutation == "ids_negative":
        value["input_ids"][0, 0] = -1
    elif mutation == "batch_two":
        value["input_ids"] = value["input_ids"].repeat(2, 1)
        value["attention_mask"] = value["attention_mask"].repeat(2, 1)
    elif mutation in {"empty_tokens", "too_many_tokens"}:
        count = 0 if mutation == "empty_tokens" else 4097
        value["input_ids"] = torch.zeros((1, count), dtype=torch.int64)
        value["attention_mask"] = torch.ones((1, count), dtype=torch.int64)
    elif mutation == "mask_float":
        value["attention_mask"] = value["attention_mask"].float()
    elif mutation == "mask_shape":
        value["attention_mask"] = value["attention_mask"][:, :2]
    elif mutation == "mask_nonbinary":
        value["attention_mask"][0, 1] = 2
    elif mutation == "mask_all_zero":
        value["attention_mask"].zero_()
    elif mutation == "grid_float":
        value["image_grid_thw"] = value["image_grid_thw"].float()
    elif mutation == "grid_missing_image":
        value["image_grid_thw"] = value["image_grid_thw"][:0]
    elif mutation == "grid_extra_image":
        value["image_grid_thw"] = value["image_grid_thw"].repeat(2, 1)
    elif mutation == "grid_temporal":
        value["image_grid_thw"][0, 0] = 2
    elif mutation == "grid_zero":
        value["image_grid_thw"][0, 1] = 0
    elif mutation == "pixels_below_min":
        value["image_grid_thw"][0] = torch.tensor([1, 4, 4])
        value["pixel_values"] = value["pixel_values"][:16]
    elif mutation == "pixels_above_max":
        value["image_grid_thw"][0] = torch.tensor([1, 64, 64])
        value["pixel_values"] = torch.zeros((4096, 96))
    elif mutation == "patch_bool":
        processor.image_processor.patch_size = True
    elif mutation == "temporal_zero":
        processor.image_processor.temporal_patch_size = 0
    elif mutation == "pixels_float64":
        value["pixel_values"] = value["pixel_values"].double()
    elif mutation == "pixels_shape":
        value["pixel_values"] = value["pixel_values"][:, :95]
    elif mutation == "pixels_nonfinite":
        value["pixel_values"][0, 0] = float("nan")
    with pytest.raises(ValueError):
        _validate(value, torch, processor=processor)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_pixels": 50175},
        {"max_pixels": True},
        {"max_tokens": 4095},
        {"max_tokens": True},
        {"image_count": True},
        {"image_count": 0},
    ],
)
def test_declared_budgets_cannot_be_relaxed_or_changed(torch_cpu, limits):
    with pytest.raises(ValueError):
        _validate(_inputs(torch_cpu), torch_cpu, **limits)


def test_valid_actor_identity_does_not_mask_different_processed_pixel_bytes(
    source_sample, tmp_path, torch_cpu
):
    processor = CPUFixtureProcessor(torch_cpu, mutate_second=True)
    with pytest.raises(ValueError, match="tensor bytes differ"):
        with NavigationSourceValidationSession(tmp_path) as session:
            session.prime((source_sample,))
            verifier.verify_sample_pair(
                source_sample,
                tmp_path,
                session,
                processor,
                actor_module=actor_module,
                pilot=pilot,
                budget=NavigationSupervisionBudget(),
                torch_module=torch_cpu,
            )
    assert len(processor.calls) == 2
    assert not session.verified


def test_altered_bound_source_is_rejected_before_any_processing(source_sample, tmp_path, torch_cpu):
    processor = CPUFixtureProcessor(torch_cpu)
    (tmp_path / source_sample.frames[-1].path).write_bytes(b"changed source bytes")
    with pytest.raises(AutonomyContractError):
        with NavigationSourceValidationSession(tmp_path) as session:
            session.prime((source_sample,))
            verifier.verify_sample_pair(
                source_sample,
                tmp_path,
                session,
                processor,
                actor_module=actor_module,
                pilot=pilot,
                budget=NavigationSupervisionBudget(),
                torch_module=torch_cpu,
            )
    assert not processor.calls and not session.verified


def _sealed_plan(tmp_path):
    (tmp_path / "artifacts/evaluation").mkdir(parents=True)
    snapshot = tmp_path / "sealed-base"
    snapshot.mkdir()
    source = tmp_path / "sealed-data"
    source.mkdir()
    bound = source / "source.json"
    bound.write_text("{}", encoding="utf-8")
    row = supervisor.file_record(bound)
    return {
        "artifact_root": str(tmp_path),
        "base": {"snapshot": str(snapshot)},
        "dataset_report": row,
        "dataset_samples": row,
        "review_receipt": row,
        "source_files": [],
    }


def test_new_output_is_exactly_one_fresh_top_level_evaluation_sibling(tmp_path):
    plan = _sealed_plan(tmp_path)
    output = tmp_path / "artifacts/evaluation/onboard-navigation-actor-inputs-fixture"
    assert (
        verifier.resolve_evaluation_output(output, tmp_path, plan, supervisor=supervisor) == output
    )
    assert not output.exists()
    output.mkdir()
    with pytest.raises(FileExistsError):
        verifier.resolve_evaluation_output(output, tmp_path, plan, supervisor=supervisor)
    assert output.is_dir() and not list(output.iterdir())


@pytest.mark.parametrize(
    "suffix",
    [
        "artifacts/training/onboard-navigation-actor-inputs-new",
        "artifacts/evaluation/wrong-name",
        "artifacts/evaluation/old-attempt/onboard-navigation-actor-inputs-new",
        "sealed-data/onboard-navigation-actor-inputs-new",
    ],
)
def test_other_or_nested_previous_evidence_outputs_are_rejected(tmp_path, suffix):
    plan = _sealed_plan(tmp_path)
    output = tmp_path / suffix
    output.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        verifier.resolve_evaluation_output(output, tmp_path, plan, supervisor=supervisor)
    assert not output.exists()


def test_output_symlink_escape_is_rejected_without_writing(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    plan = _sealed_plan(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "artifacts/evaluation/link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"This Windows session cannot create test symlinks: {exc}")
    with pytest.raises(ValueError):
        verifier.resolve_evaluation_output(
            link / "onboard-navigation-actor-inputs-new", root, plan, supervisor=supervisor
        )
    assert not list(outside.iterdir())


def test_source_capture_deduplicates_declarations_and_rejects_changed_bytes(tmp_path):
    path = tmp_path / "large-annotation.json"
    path.write_text("Bound annotation", encoding="utf-8")
    row = supervisor.file_record(path)
    relative = row | {"path": path.name}
    sample = SimpleNamespace(
        source=SimpleNamespace(to_mapping=lambda: {"annotation": relative, "pose": row}),
        frames=(SimpleNamespace(path=path.name, bytes=row["bytes"], sha256=row["sha256"]),),
    )
    calls = []

    def bound(value, root):
        calls.append(value["path"])
        return pilot.verify_binding(value, root)

    facade = SimpleNamespace(
        binding_shape=pilot.binding_shape,
        plan_bindings=lambda plan: [row, relative],
        verify_binding=bound,
    )
    kwargs = dict(
        pilot=facade,
        supervisor=supervisor,
        processor=object(),
        actor_module=actor_module,
        torch_module=SimpleNamespace(__file__=__file__),
    )
    records = verifier.capture_sources(
        {"artifact_root": str(tmp_path)}, path, [sample] * 86, **kwargs
    )
    assert calls == [str(path)]
    assert len([r for r in records if r["path"] == str(path)]) == 1
    path.write_text("Changed annotation", encoding="utf-8")
    with pytest.raises(ValueError, match="bytes or SHA changed"):
        verifier.capture_sources({"artifact_root": str(tmp_path)}, path, [sample], **kwargs)
    conflicting = row | {"sha256": "0" * 64}
    facade.plan_bindings = lambda plan: [row, conflicting]
    with pytest.raises(ValueError, match="conflicting"):
        verifier.capture_sources({"artifact_root": str(tmp_path)}, path, [], **kwargs)


@pytest.mark.parametrize("after_change", [None, "source_file", "annotation"])
def test_injected_cpu_run_preserves_report_scope_and_final_source_gates(
    source_sample, tmp_path, torch_cpu, monkeypatch, after_change
):
    # This fixture replaces ONLY original plan/dataset admission. It is not the
    # sealed 86-sample run; both actor factories/processor bytes/session remain real.
    plan = _sealed_plan(tmp_path)
    samples = tuple(replace(source_sample, sample_id=f"fixture-{i}") for i in range(86))
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for name in (
        "test_verify_onboard_navigation_actor_inputs.py",
        "test_onboard_navigation_actor.py",
    ):
        (tests_dir / name).write_text("Bound fixture source", encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setattr(verifier, "PLAN_SHA256", plan_sha)
    monkeypatch.setattr(pilot, "load_plan", lambda path, sha: plan)
    monkeypatch.setattr(pilot, "verify_plan_sources", lambda value: None)
    monkeypatch.setattr(pilot, "admit_dataset", lambda value: (samples, ["fixture-gold"] * 86))
    monkeypatch.setattr(pilot, "plan_bindings", lambda value: [plan["dataset_samples"]])
    processor = CPUFixtureProcessor(torch_cpu)
    pair = verifier.verify_sample_pair
    completed = []

    def checked_pair(*args, **kwargs):
        result = pair(*args, **kwargs)
        completed.append(result)
        if len(completed) == 86 and after_change:
            path = (
                tests_dir / "test_onboard_navigation_actor.py"
                if after_change == "source_file"
                else tmp_path / source_sample.source.annotation.path
            )
            path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(verifier, "verify_sample_pair", checked_pair)
    output = tmp_path / "artifacts/evaluation/onboard-navigation-actor-inputs-fixture"
    if after_change:
        with pytest.raises((ValueError, AutonomyContractError)):
            verifier.verify_actor_inputs(
                plan_path, plan_sha, output, processor_factory=lambda snapshot, budget: processor
            )
        assert not output.exists()
        assert len(completed) == 86
        return
    report = verifier.verify_actor_inputs(
        plan_path, plan_sha, output, processor_factory=lambda snapshot, budget: processor
    )
    assert json.loads((output / "report.json").read_bytes()) == report
    assert report["sample_count"] == 86 and len(report["samples"]) == 86
    assert report["all_86_prefix_pairs_equal"] and report["source_validation_session_verified"]
    assert report["source_bindings_before"] == report["source_bindings_after"]
    assert report["actor_contract"] == actor_module.ACTOR_CONTRACT
    assert (
        report["system_prompt_sha256"]
        == hashlib.sha256(actor_module.ACTOR_SYSTEM_PROMPT.encode()).hexdigest()
    )
    assert report["retained_source_projection"] is True
    assert report["official_processor_used"] is False
    assert (
        report["processor"]["loading_scope"]
        == "injected_cpu_test_processor_not_official_acceptance"
    )
    assert report["processor"]["local_files_only"] is None
    assert report["processor"]["transformers_version"] is None
    for field in (
        "independently_captured_live_observations",
        "old_training_prefix_identity_claimed",
        "actual_live_camera_parity_verified",
        "sim_real_parity_verified",
        "training_completed",
        "model_loaded",
        "model_inference_performed",
        "optimizer_performed",
        "quality_claim",
        "candidate_promoted",
        "execution_authority",
        "final_release_allowed",
    ):
        assert report[field] is False
    assert len(processor.calls) == 172 and not torch_cpu.cuda.is_initialized()
    with pytest.raises(FileExistsError):
        verifier.verify_actor_inputs(
            plan_path, plan_sha, output, processor_factory=lambda snapshot, budget: processor
        )
    assert len(processor.calls) == 172


def test_official_processor_is_local_only_pinned_and_not_a_model(monkeypatch, tmp_path):
    calls = []

    class Qwen3VLProcessor:
        pass

    def load(*args, **kwargs):
        calls.append((args, kwargs))
        return Qwen3VLProcessor()

    runtime = SimpleNamespace(
        __version__="4.57.6", AutoProcessor=SimpleNamespace(from_pretrained=load)
    )
    monkeypatch.setitem(sys.modules, "transformers", runtime)
    result = verifier.official_processor(tmp_path, pilot.NavigationTrainingBudget())
    assert type(result).__name__ == "Qwen3VLProcessor"
    assert calls == [
        (
            (str(tmp_path),),
            {
                "local_files_only": True,
                "trust_remote_code": False,
                "max_pixels": 50176,
            },
        )
    ]
    runtime.__version__ = "unknown"
    with pytest.raises(ValueError, match="pinned Transformers"):
        verifier.official_processor(tmp_path, pilot.NavigationTrainingBudget())
    assert len(calls) == 1


def test_static_import_and_wrong_plan_cli_do_not_import_heavy_backends(tmp_path):
    script = Path(verifier.__file__).resolve()
    code = (
        "import importlib.util,sys; "
        f"spec=importlib.util.spec_from_file_location('prefix_verifier',{str(script)!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert not {'torch','transformers','PIL'} & set(sys.modules); "
        "assert module.main(['--plan','missing.json','--plan-sha256','wrong',"
        f"'--output-dir',{str(tmp_path / 'absent')!r}]) == 1; "
        "assert not {'torch','transformers','PIL'} & set(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["event"] == "actor_prefix_verification_rejected"
    assert not (tmp_path / "absent").exists()

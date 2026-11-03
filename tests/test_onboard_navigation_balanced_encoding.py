"""Injected CPU fixtures are encoding checks, NOT official processor/model evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboard_navigation_actor import source_sample as source_sample
from test_verify_onboard_navigation_actor_inputs import CPUFixtureProcessor, _inputs

from multidetect.autonomy import onboard_navigation_actor as actor_module
from multidetect.autonomy import onboard_navigation_supervision as navigation
from multidetect.autonomy.serialization import AutonomyContractError
from scripts import onboard_navigation_balanced_encoding as encoding
from scripts import train_onboard_vlm_navigation_pilot as pilot
from scripts import verify_onboard_navigation_actor_inputs as checker


@pytest.fixture(scope="module")
def torch_cpu():
    torch = pytest.importorskip(
        "torch", reason="navigation encoding CPU fixtures require optional PyTorch"
    )
    assert not torch.cuda.is_initialized()
    return torch


class BalancedCPUProcessor(CPUFixtureProcessor):
    """Consumes both messages and kwargs; deterministic bytes, no actual tokenizer."""

    def __init__(self, torch, *, mutation=None, prefix_length=3, suffix=None):
        super().__init__(torch)
        self.mutation = mutation
        self.prefix_length = prefix_length
        self.suffix = [41, 17, 47, 18, 43, 19, 45, 0] if suffix is None else suffix
        specials = dict(
            zip(
                ("<|image_pad|>", "<|video_pad|>", "<|vision_start|>", "<|vision_end|>"),
                (17, 18, 19, 47),
                strict=True,
            )
        )
        self.tokenizer.convert_tokens_to_ids = lambda value: specials[value]

    def apply_chat_template(self, messages, **kwargs):
        generation = kwargs["add_generation_prompt"]
        assert kwargs["tokenize"] is True and kwargs["return_dict"] is True
        assert kwargs["return_tensors"] == "pt"
        options = kwargs["images_kwargs"]
        assert options == {"min_pixels": 1024, "max_pixels": 50176}
        self.kwargs_references.append(options)
        options.clear()
        assert [message["role"] for message in messages] == (
            ["system", "user"] if generation else ["system", "user", "assistant"]
        )
        texts, images = [], []
        for message in messages[:2]:
            for item in message["content"]:
                if item["type"] == "text":
                    texts.append(item["text"])
                else:
                    image = item["image"]
                    assert image.mode == "RGB" and not image.info
                    images.append(image)
                    texts.append(hashlib.sha256(image.tobytes()).hexdigest())
        target = None if generation else messages[-1]["content"][0]["text"]
        self.calls.append({"texts": texts, "target": target, "generation": generation})
        self.images.extend(images)
        token = int(hashlib.sha256("\n".join(texts).encode()).hexdigest()[:12], 16)
        torch = self.torch
        result = _inputs(torch, image_count=len(images), token=token)
        if self.prefix_length != 3:
            result["input_ids"] = torch.full((1, self.prefix_length), token, dtype=torch.int64)
            result["attention_mask"] = torch.ones((1, self.prefix_length), dtype=torch.int64)
        if not generation:
            result["input_ids"] = torch.cat(
                [result["input_ids"], torch.tensor([self.suffix], dtype=torch.int64)], dim=1
            )
            result["attention_mask"] = torch.ones_like(result["input_ids"])
            if self.suffix and self.suffix[-1] == 0:
                result["attention_mask"][0, -1] = 0
            self._mutate(result)
        for message in messages:
            message["content"].clear()
        return result

    def _mutate(self, value):
        torch, mutation = self.torch, self.mutation
        if mutation == "ids":
            value["input_ids"][0, 0] += 1
        elif mutation == "mask":
            value["attention_mask"][0, 0] = 0
        elif mutation == "grid":
            value["image_grid_thw"][:, 1:] = torch.tensor([4, 16])
        elif mutation == "pixels":
            value["pixel_values"][0, 0] += 1
        elif mutation == "extra":
            value["pose"] = torch.zeros(1)
        elif mutation == "missing":
            del value["attention_mask"]
        elif mutation == "non_tensor":
            value["pixel_values"] = []
        elif mutation == "autograd":
            value["pixel_values"].requires_grad_(True)
        elif mutation == "meta":
            value["pixel_values"] = torch.empty(value["pixel_values"].shape, device="meta")
        elif mutation == "mask_nonbinary":
            value["attention_mask"][0, -1] = 2
        elif mutation == "ids_float":
            value["input_ids"] = value["input_ids"].float()
        elif mutation == "ids_negative":
            value["input_ids"][0, -1] = -1
        elif mutation == "pixels_double":
            value["pixel_values"] = value["pixel_values"].double()
        elif mutation == "pixels_nonfinite":
            value["pixel_values"][0, 0] = float("nan")
        elif mutation == "pixels_shape":
            value["pixel_values"] = value["pixel_values"][:, :95]
        elif mutation == "grid_float":
            value["image_grid_thw"] = value["image_grid_thw"].float()
        elif mutation == "grid_missing":
            value["image_grid_thw"] = value["image_grid_thw"][:0]
        elif mutation == "processor_failure":
            raise RuntimeError("Injected CPU processor failure")


def _budget(**changes):
    def old_validator():
        raise AssertionError("Never invoke the original optimizer-budget validator")

    return SimpleNamespace(
        **(
            {
                "max_pixels_per_image": 50176,
                "max_input_tokens": 4096,
                "max_new_tokens": 192,
                "max_optimizer_steps": 280,
                "validate": old_validator,
            }
            | changes
        )
    )


def _reference(sample, root, torch):
    # A local injected reference, never the sealed official CPU86 report.
    return checker.verify_sample_pair(
        sample,
        root,
        None,
        CPUFixtureProcessor(torch),
        actor_module=actor_module,
        pilot=pilot,
        budget=navigation.NavigationSupervisionBudget(),
        torch_module=torch,
    )


def _encode(sample, root, torch, *, processor=None, reference=None, budget=None, session=None):
    return encoding.encode_navigation_sample(
        sample,
        root,
        processor=processor or BalancedCPUProcessor(torch),
        torch_module=torch,
        budget=budget or _budget(),
        prefix_reference=(_reference(sample, root, torch) if reference is None else reference),
        session=session,
    )


@pytest.mark.parametrize("image_count", [1, 2])
def test_fresh_actor_prefix_full_response_mask_and_backend_shape(
    source_sample, tmp_path, torch_cpu, image_count
):
    sample = replace(source_sample, frames=source_sample.frames[-image_count:])
    processor = BalancedCPUProcessor(torch_cpu)
    reference = _reference(sample, tmp_path, torch_cpu)
    with navigation.NavigationSourceValidationSession(tmp_path) as session:
        session.prime((sample,))
        result = _encode(
            sample, tmp_path, torch_cpu, processor=processor, reference=reference, session=session
        )
        assert not session.verified  # The helper never promotes provisional source data.
    assert session.verified
    assert set(result) == {
        "prefix",
        "full",
        "labels",
        "prefix_tokens",
        "total_tokens",
        "response_tokens",
        "supervised_tokens",
        "prefix_tensors",
        "input_tensors",
        "response_labels_sha256",
        "target_sha256",
        "actor_identity_sha256",
        "prefix_reference_verified",
        "prompt_label_mask_verified",
    }
    assert result["prefix_tokens"] == 3 and result["total_tokens"] == 11
    assert result["response_tokens"] == 8 and result["supervised_tokens"] == 3
    assert result["labels"].tolist() == [[-100] * 3 + [41, -100, -100, -100, 43, -100, 45, -100]]
    assert result["labels"].shape == result["full"]["input_ids"].shape
    assert result["labels"].dtype == torch_cpu.int64
    assert result["labels"].device.type == "cpu" and not result["labels"].requires_grad
    assert result["prefix_tensors"] == reference["training_prefix"]["tensors"]
    assert result["input_tensors"] == {
        key: pilot.tensor_record(value, torch_cpu) for key, value in result["full"].items()
    }
    assert result["prefix_reference_verified"] is True
    assert result["prompt_label_mask_verified"] is True
    assert result["actor_identity_sha256"] == reference["actor_identity_sha256"]
    assert (
        result["response_labels_sha256"]
        == hashlib.sha256(pilot.core.canonical_bytes(result["labels"][0].tolist())).hexdigest()
    )
    target = navigation.serialize_navigation_supervision_target(sample, tmp_path)
    assert result["target_sha256"] == hashlib.sha256(target.encode()).hexdigest()
    assert processor.calls[0]["texts"] == processor.calls[1]["texts"]
    assert processor.calls[0]["target"] is None and processor.calls[1]["target"] == target
    assert processor.kwargs_references[0] is not processor.kwargs_references[1]
    assert len({id(image) for image in processor.images}) == 2 * image_count
    for image in processor.images:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))
    actor_text = "\n".join(processor.calls[0]["texts"])
    for forbidden in (
        sample.sample_id,
        sample.episode_id,
        sample.scene_id,
        "opaque-frame-",
        "secret-gold-goal",
        "imageid",
        "source_xy_z_up_ccw_yaw",
    ):
        assert forbidden not in actor_text
    # Backend loss/generate consume exactly these keys; no model was run here.
    assert set(result["prefix"]) == set(result["full"]) == checker.FIELDS
    assert result["response_tokens"] + 1 <= result["total_tokens"]
    assert not torch_cpu.cuda.is_initialized()


@pytest.mark.parametrize(
    "mutation",
    [
        "ids",
        "mask",
        "grid",
        "pixels",
        "extra",
        "missing",
        "non_tensor",
        "autograd",
        "meta",
        "mask_nonbinary",
        "ids_float",
        "ids_negative",
        "pixels_double",
        "pixels_nonfinite",
        "pixels_shape",
        "grid_float",
        "grid_missing",
        "processor_failure",
    ],
)
def test_actual_full_tensors_and_exact_prefix_cannot_drift(
    source_sample, tmp_path, torch_cpu, mutation
):
    processor = BalancedCPUProcessor(torch_cpu, mutation=mutation)
    with pytest.raises((ValueError, RuntimeError)):
        _encode(source_sample, tmp_path, torch_cpu, processor=processor)
    for image in processor.images:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))
    assert not torch_cpu.cuda.is_initialized()


@pytest.mark.parametrize(
    "field",
    [
        "sample_id",
        "actor_identity_sha256",
        "source_prompt_sha256",
        "source_frames",
        "retained_history_valid",
        "actor_identities_equal",
        "training_prefix",
        "observable_prefix",
        "all_four_tensor_bytes_equal",
        "labels_pose_future_ids_entered_model_messages",
        "unknown",
    ],
)
def test_complete_reference_not_only_token_sha_is_bound(source_sample, tmp_path, torch_cpu, field):
    reference = copy.deepcopy(_reference(source_sample, tmp_path, torch_cpu))
    if field == "source_frames":
        reference[field][0]["path"] = "another-source.png"
    elif field in {"training_prefix", "observable_prefix"}:
        reference[field]["patch_size"] += 1
    elif field == "retained_history_valid":
        # Plain equality would incorrectly accept bool/int substitution.
        reference[field] = 1
    else:
        reference[field] = "altered"
    with pytest.raises(ValueError, match="Complete CPU86 prefix reference row"):
        _encode(source_sample, tmp_path, torch_cpu, reference=reference)


@pytest.mark.parametrize("suffix", [[], [41] * 193, [17, 18, 19, 47, 0]])
def test_empty_oversized_or_wholly_masked_response_rejected(
    source_sample, tmp_path, torch_cpu, suffix
):
    with pytest.raises(ValueError):
        _encode(
            source_sample,
            tmp_path,
            torch_cpu,
            processor=BalancedCPUProcessor(torch_cpu, suffix=suffix),
        )


@pytest.mark.parametrize(
    "name,value",
    [
        ("max_pixels_per_image", 50175),
        ("max_pixels_per_image", True),
        ("max_input_tokens", 4097),
        ("max_input_tokens", 4096.0),
        ("max_new_tokens", 193),
        ("max_new_tokens", False),
    ],
)
def test_only_explicit_new_encoding_limits_are_admitted(name, value):
    with pytest.raises(ValueError, match="50176/4096/192"):
        encoding.validate_encoding_budget(_budget(**{name: value}))


def test_full_4096_and_suffix_192_boundaries_no_truncation(source_sample, tmp_path, torch_cpu):
    sample = source_sample
    processor = BalancedCPUProcessor(torch_cpu, prefix_length=3904, suffix=[41] * 192)
    actor = actor_module.NavigationActorInput.from_supervision_sample(sample, tmp_path)
    prefix = checker.process_actor(actor, processor, actor_module)
    info = checker.validate_prefix_inputs(
        prefix,
        processor=processor,
        image_count=2,
        torch_module=torch_cpu,
        tensor_record=pilot.tensor_record,
    )
    reference = _reference(sample, tmp_path, torch_cpu)
    reference["training_prefix"] = reference["observable_prefix"] = info
    processor = BalancedCPUProcessor(torch_cpu, prefix_length=3904, suffix=[41] * 192)
    result = _encode(sample, tmp_path, torch_cpu, processor=processor, reference=reference)
    assert result["total_tokens"] == 4096 and result["response_tokens"] == 192
    assert result["supervised_tokens"] == 192
    with pytest.raises(ValueError, match="bounded int64"):
        _encode(
            sample,
            tmp_path,
            torch_cpu,
            reference=reference,
            processor=BalancedCPUProcessor(torch_cpu, prefix_length=3905, suffix=[41] * 192),
        )


def test_altered_source_fails_before_processor(source_sample, tmp_path, torch_cpu):
    reference = _reference(source_sample, tmp_path, torch_cpu)
    processor = BalancedCPUProcessor(torch_cpu)
    (tmp_path / source_sample.frames[-1].path).write_bytes(b"changed")
    with pytest.raises(AutonomyContractError):
        _encode(source_sample, tmp_path, torch_cpu, processor=processor, reference=reference)
    assert not processor.calls


def test_source_drift_on_final_session_exit_is_not_helper_acceptance(
    source_sample, tmp_path, torch_cpu
):
    reference = _reference(source_sample, tmp_path, torch_cpu)
    with pytest.raises(AutonomyContractError):
        with navigation.NavigationSourceValidationSession(tmp_path) as session:
            session.prime((source_sample,))
            _encode(source_sample, tmp_path, torch_cpu, reference=reference, session=session)
            path = tmp_path / source_sample.source.annotation.path
            path.write_bytes(path.read_bytes() + b" ")
    assert not session.verified


def test_pixel_prefix_equality_is_byte_exact_even_for_signed_zero(
    source_sample, tmp_path, torch_cpu
):
    class ZeroCPUProcessor(BalancedCPUProcessor):
        def apply_chat_template(self, messages, **kwargs):
            generation = kwargs["add_generation_prompt"]
            result = super().apply_chat_template(messages, **kwargs)
            result["pixel_values"][0, 0] = 0.0 if generation else -0.0
            return result

    processor = ZeroCPUProcessor(torch_cpu)
    actor = actor_module.NavigationActorInput.from_supervision_sample(source_sample, tmp_path)
    prefix = checker.process_actor(actor, processor, actor_module)
    info = checker.validate_prefix_inputs(
        prefix,
        processor=processor,
        image_count=2,
        torch_module=torch_cpu,
        tensor_record=pilot.tensor_record,
    )
    reference = _reference(source_sample, tmp_path, torch_cpu)
    reference["training_prefix"] = reference["observable_prefix"] = info
    assert torch_cpu.equal(torch_cpu.tensor([0.0]), torch_cpu.tensor([-0.0]))
    with pytest.raises(ValueError, match="exact token/mask/grid/pixel prefix bytes"):
        _encode(
            source_sample,
            tmp_path,
            torch_cpu,
            reference=reference,
            processor=ZeroCPUProcessor(torch_cpu),
        )


def test_missing_visual_token_ids_fail_closed(source_sample, tmp_path, torch_cpu):
    processor = BalancedCPUProcessor(torch_cpu)
    processor.tokenizer.convert_tokens_to_ids = lambda token: None
    with pytest.raises(ValueError, match="all four visual special tokens"):
        _encode(source_sample, tmp_path, torch_cpu, processor=processor)


def test_response_only_label_function_cannot_accidentally_unmask_prompt(
    source_sample, tmp_path, torch_cpu, monkeypatch
):
    original = pilot.core.response_only_labels

    def unmasked(*args):
        labels = original(*args)
        labels[0] = args[0][0]
        return labels

    monkeypatch.setattr(pilot.core, "response_only_labels", unmasked)
    with pytest.raises(ValueError, match="Prompt, image and padding tokens"):
        _encode(source_sample, tmp_path, torch_cpu)


def test_static_import_is_stdlib_only_and_rejects_budget_without_heavy_imports():
    path = Path(encoding.__file__).resolve()
    code = (
        "import importlib.util,sys; from types import SimpleNamespace; "
        f"spec=importlib.util.spec_from_file_location('balanced_encoding',{str(path)!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "assert not {'torch','transformers','PIL'} & set(sys.modules); "
        "module.validate_encoding_budget(SimpleNamespace(max_pixels_per_image=50176,"
        "max_input_tokens=4096,max_new_tokens=192,max_optimizer_steps=280)); "
        "assert not {'torch','transformers','PIL'} & set(sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr

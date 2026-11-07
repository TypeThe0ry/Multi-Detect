from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts import train_onboard_vlm_grounding as grounding


def target(visible=True):
    return {
        "target_reference_id": "e" * 32,
        "current_target_visibility": "visible" if visible else "not_confirmed",
        "gps": "unknown",
        "ned": "unknown",
        "aircraft_motion": "unknown",
    }


def dataset(tmp_path):
    samples = []
    for index, split in enumerate(("train", "val", "test")):
        images = []
        for role in ("reference", "current"):
            path = tmp_path / f"{split}-{role}.png"
            Image.new("RGB", (64, 48), (15 + index, 27, 39)).save(path)
            images.append(
                {
                    "image_id": f"{index:032x}",
                    "role": role,
                    "path": path.name,
                    "sha256": grounding.probe.sha256_file(path),
                    "bytes": path.stat().st_size,
                    "width": 64,
                    "height": 48,
                    "encoding": "png",
                }
            )
        samples.append(
            {
                "sample_id": f"{index:032x}",
                "split": split,
                "model_input": {
                    "prompt": "比较指代对象，保持未知事实。",
                    "target_reference_id": "e" * 32,
                    "images": images,
                    "context": {"gps": "unknown", "ned": "unknown", "aircraft_motion": "unknown"},
                },
                "assistant_target": target(),
                "model_input_sha256": "a" * 64,
                "assistant_target_sha256": "b" * 64,
                "provenance": {
                    "source_clip_id": "secret-condition-full-occlusion",
                    "world": "SECRET_WORLD",
                    "truth": "SECRET_MASK_LABEL",
                },
            }
        )
    return {"samples": samples}


@pytest.mark.parametrize(
    "name,value",
    [
        ("epochs", 0),
        ("epochs", 6),
        ("epochs", True),
        ("max_optimizer_steps", 201),
        ("max_pixels_per_image", 500000),
        ("max_input_tokens", 9000),
        ("max_new_tokens", 257),
        ("rank", 9),
        ("alpha", 0),
        ("learning_rate", float("nan")),
        ("learning_rate", 0.01),
        ("learning_rate", True),
        ("seed", -1),
        ("gradient_checkpointing", 1),
    ],
)
def test_budget_bounded(name, value):
    with pytest.raises(ValueError):
        replace(grounding.TrainingBudget(), **{name: value}).validate()


def test_exact_preregistered_plan_and_budget():
    path = (
        Path(grounding.__file__).parents[1]
        / "configs/training/onboard-vlm-grounding-lora-dev-v1.json"
    )
    document = grounding.load_training_plan(path, grounding.PLAN_SHA256)
    assert grounding.budget_from_plan(document) == grounding.TrainingBudget()
    with pytest.raises(ValueError, match="Unknown or changed"):
        grounding.load_training_plan(path, "0" * 64)


def test_plan_byte_mutation_not_admitted(tmp_path):
    path = tmp_path / "plan.json"
    path.write_bytes(b'{"optimization":{"epochs":1}}')
    with pytest.raises(ValueError, match="Unknown or changed"):
        grounding.load_training_plan(path, grounding.probe.sha256_file(path))


def test_response_only_exact_prefix_images_and_padding():
    full = [11, 22, 99, 33, 44, 55, 0, 0]
    labels = grounding.response_only_labels(full, full[:4], [1, 1, 1, 1, 1, 1, 0, 0], {99})
    assert labels == [-100, -100, -100, -100, 44, 55, -100, -100]


def test_image_special_token_in_suffix_never_supervised():
    assert grounding.response_only_labels([1, 2, 99, 3], [1, 2], [1] * 4, {99}) == [
        -100,
        -100,
        -100,
        3,
    ]


@pytest.mark.parametrize(
    "full,prefix,mask",
    [
        ([1, 2], [], [1, 1]),
        ([1, 2], [1, 2], [1, 1]),
        ([1, 2, 3], [1, 4], [1, 1, 1]),
        ([1, 2, 3], [1, 2], [1]),
        ([1, 2, 3], [1, 2], [1, 1, 2]),
        ([1, 2, 3], [1, 2], [1, 1, 0]),
    ],
)
def test_loss_mask_boundary_fail_closed(full, prefix, mask):
    with pytest.raises(ValueError):
        grounding.response_only_labels(full, prefix, mask, set())


def test_admission_independent_splits_and_no_over_budget_truncation(tmp_path):
    document = dataset(tmp_path)
    admitted = grounding.admit_samples(document, grounding.TrainingBudget())
    assert list(admitted) == ["train", "val", "test"]
    assert all(len(rows) == 1 for rows in admitted.values())
    with pytest.raises(ValueError, match="no truncation"):
        grounding.admit_samples(
            document, replace(grounding.TrainingBudget(), epochs=2, max_optimizer_steps=1)
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("gps", [1, 2]),
        ("ned", {"north": 3}),
        ("aircraft_motion", "stationary"),
        ("current_target_visibility", []),
        ("current_target_visibility", {}),
        ("current_target_visibility", "absent"),
        ("target_reference_id", "other"),
    ],
)
def test_no_invented_motion_positions_or_visibility_enum(field, value):
    answer = target()
    answer[field] = value
    with pytest.raises(ValueError):
        grounding.validate_target(answer, "e" * 32)


def test_no_action_labels_or_three_image_silent_drop(tmp_path):
    document = dataset(tmp_path)
    document["samples"][0]["assistant_target"]["flight_plan"] = {"action": "search"}
    with pytest.raises(ValueError, match="fixed response"):
        grounding.admit_samples(document, grounding.TrainingBudget())
    document = dataset(tmp_path)
    document["samples"][0]["model_input"]["images"].insert(
        1, {**document["samples"][0]["model_input"]["images"][0], "role": "history"}
    )
    with pytest.raises(ValueError, match="no silent drop"):
        grounding.admit_samples(document, grounding.TrainingBudget())


def test_truth_and_split_not_model_visible(tmp_path):
    sample = dataset(tmp_path)["samples"][1]
    first, processing = grounding.image_messages(sample, tmp_path, grounding.TrainingBudget())
    second_sample = copy.deepcopy(sample)
    second_sample["provenance"] = {"pose": "MALICIOUS_SECRET_GOLD"}
    second_sample["split"] = "other"
    second, _processing = grounding.image_messages(
        second_sample, tmp_path, grounding.TrainingBudget()
    )
    text = "\n".join(
        item["text"] for message in first for item in message["content"] if item["type"] == "text"
    )
    other = "\n".join(
        item["text"] for message in second for item in message["content"] if item["type"] == "text"
    )
    assert text == other
    assert all(term not in text for term in ("SECRET", "occlusion", "train", "val", "test"))
    assert sample["model_input"]["prompt"] in text
    assert len(processing) == 2
    assert [p["role"] for p in processing] == ["reference", "current"]


def test_changed_rgb_refused(tmp_path):
    sample = dataset(tmp_path)["samples"][0]
    path = tmp_path / sample["model_input"]["images"][1]["path"]
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="Bound RGB changed"):
        grounding.image_messages(sample, tmp_path, grounding.TrainingBudget())


def metric_row(identifier, visible, predicted, *, valid=True):
    answer = (
        target(visible=predicted)
        if isinstance(predicted, bool)
        else {
            **target(),
            "current_target_visibility": predicted,
        }
    )
    return {
        "sample_id": identifier,
        "split": "test",
        "source_clip_id": "clip-one",
        "reference_rgb_sha256": "a" * 64,
        "current_rgb_sha256": hashlib.sha256(identifier.encode()).hexdigest(),
        "prompt_sha256": "b" * 64,
        "expected_target": target(visible),
        "parsed_output": answer,
        "strict_fact_reply_accepted": valid,
    }


def test_primary_visibility_not_inflated_by_unknown_facts():
    rows = [metric_row("positive", True, False), metric_row("zero", False, False)]
    metrics = grounding.grounding_metrics(rows)
    assert metrics["visibility_confusion"]["balanced_accuracy"] == 0.5
    assert metrics["unknown_navigation_fact_accuracy"] == 1.0
    assert metrics["unknown_facts_included_in_visibility_accuracy"] is False
    assert metrics["consistent_swap_pair_count"] == 0


def test_strict_invalid_visibility_field_not_counted_tp_or_tn():
    rows = [
        metric_row("positive", True, True, valid=False),
        metric_row("zero", False, False, valid=False),
    ]
    metrics = grounding.grounding_metrics(rows)
    assert metrics["visibility_confusion"] == {
        "tp": 0,
        "tn": 0,
        "fp": 1,
        "fn": 1,
        "invalid": 2,
        "sensitivity": 0.0,
        "specificity": 0.0,
        "balanced_accuracy": 0.0,
    }


@pytest.mark.parametrize("malformed", [[], {}, None, 1])
def test_malformed_visibility_metric_not_crash(malformed):
    metrics = grounding.grounding_metrics([metric_row("one", True, malformed, valid=False)])
    assert metrics["visibility_confusion"]["invalid"] == 1


def test_current_rgb_swap_requires_same_prompt_reference_and_target():
    positive = metric_row("positive", True, True)
    negative = metric_row("negative", False, False)
    metrics = grounding.grounding_metrics([positive, negative])
    assert metrics["swap_pair_count"] == metrics["consistent_swap_pair_count"] == 1
    assert metrics["swap_pairs_are_independent_observations"] is False
    negative["reference_rgb_sha256"] = "c" * 64
    assert grounding.grounding_metrics([positive, negative])["swap_pair_count"] == 0


def test_eval_raw_invalid_no_repair_and_parent_flag(tmp_path, capsys):
    sample = dataset(tmp_path)["samples"][1]
    raw = "```json\n" + json.dumps(target()) + "\n```"
    calls = []

    class Backend:
        def generate(self, encoded, *, parent):
            calls.append(parent)
            return {
                "raw_output": raw,
                "output_token_ids": [1, 2],
                "generated_tokens": 2,
                "token_cap_reached": False,
                "elapsed_s": 0.01,
            }

    result = grounding.evaluate_rows(Backend(), [sample], {sample["sample_id"]: {}}, parent=True)
    assert calls == [True]
    assert result["strict_reply_count"] == result["exact_match_count"] == 0
    assert result["samples"][0]["raw_output"] == raw
    progress = json.loads(capsys.readouterr().out)
    assert progress["completed"] == progress["total"] == 1
    assert progress["phase"] == "parent"


def test_actual_tokenizer_processor_response_prefix_and_pixel_cap(tmp_path):
    torch = pytest.importorskip("torch", reason="CPU tokenizer fixture requires optional torch")
    transformers = pytest.importorskip(
        "transformers", reason="CPU tokenizer fixture requires optional transformers"
    )
    from transformers import AutoProcessor

    if not hasattr(transformers, "Qwen3VLForConditionalGeneration"):
        pytest.skip("Qwen3-VL processor requires the isolated .venv-vlm, not .venv-train")
    snapshot = Path(grounding.__file__).parents[1] / (
        "artifacts/cache/huggingface-vlm/models--Qwen--Qwen3-VL-2B-Instruct/snapshots/"
        + grounding.REVISION
    )
    if not snapshot.is_dir():
        pytest.skip("Pinned local tokenizer/processor is unavailable; no download in unit tests")
    backend = grounding.GroundingBackend.__new__(grounding.GroundingBackend)
    backend.torch = torch
    backend.budget = grounding.TrainingBudget()
    backend.processor = AutoProcessor.from_pretrained(
        str(snapshot),
        local_files_only=True,
        trust_remote_code=False,
        max_pixels=backend.budget.max_pixels_per_image,
    )
    encoded = backend.encode(dataset(tmp_path)["samples"][0], tmp_path)
    labels = encoded["labels"][0].tolist()
    full_ids = encoded["full"]["input_ids"][0].tolist()
    assert all(label == -100 for label in labels[: encoded["prefix_tokens"]])
    assert any(label != -100 for label in labels[encoded["prefix_tokens"] :])
    assert all(
        label == -100 or label == token for label, token in zip(labels, full_ids, strict=True)
    )
    assert len(encoded["actual_image_pixels"]) == 2
    assert max(encoded["actual_image_pixels"]) <= backend.budget.max_pixels_per_image


def tiny_qwen():
    from transformers import Qwen3VLConfig, Qwen3VLForConditionalGeneration

    config = Qwen3VLConfig(
        text_config={
            "vocab_size": 128,
            "hidden_size": 16,
            "intermediate_size": 32,
            "num_hidden_layers": 2,
            "num_attention_heads": 2,
            "num_key_value_heads": 1,
            "head_dim": 8,
            "rope_scaling": {"rope_type": "default", "mrope_section": [1, 1, 2]},
        },
        vision_config={
            "depth": 1,
            "hidden_size": 16,
            "intermediate_size": 32,
            "num_heads": 2,
            "out_hidden_size": 16,
            "deepstack_visual_indexes": [],
        },
    )
    return Qwen3VLForConditionalGeneration(config)


def test_real_peft_tiny_language_only_and_base_digest_proof():
    torch = pytest.importorskip("torch", reason="CPU LoRA fixture requires optional torch")

    peft = pytest.importorskip("peft", minversion="0.17.1")
    torch.manual_seed(17)
    base = tiny_qwen()
    base.requires_grad_(False)
    modules = [
        name for name, _ in base.named_modules() if grounding.LANGUAGE_TARGET.fullmatch(name)
    ]
    assert len(modules) == 4
    model = peft.get_peft_model(
        base,
        peft.LoraConfig(
            task_type="CAUSAL_LM",
            r=4,
            lora_alpha=8,
            target_modules=modules,
            bias="none",
        ),
    )
    audit = grounding.audit_trainable_parameters(model)
    assert audit["trainable_parameter_count"] > 0
    assert all(".visual." not in row["name"] for row in audit["trainable_parameters"])
    initial = grounding.parameter_digest(model, trainable=False)
    with torch.no_grad():
        for parameter in model.parameters():
            if parameter.requires_grad:
                parameter.add_(1)
    assert initial == grounding.parameter_digest(model, trainable=False)
    with torch.no_grad():
        model.base_model.model.visual.patch_embed.proj.weight.add_(1)
    assert initial != grounding.parameter_digest(model, trainable=False)


def test_non_lora_or_vision_trainability_rejected():
    fake = SimpleNamespace(
        named_parameters=lambda: [
            (
                "base_model.model.visual.q_proj.weight",
                SimpleNamespace(requires_grad=True, numel=lambda: 4, dtype="float32"),
            ),
        ]
    )
    with pytest.raises(ValueError, match="non-language"):
        grounding.audit_trainable_parameters(fake)


def test_response_window_loss_and_gradient_equal_full_masked_causal_loss():
    torch = pytest.importorskip("torch", reason="CPU gradient check requires optional torch")

    torch.manual_seed(32)
    logits = torch.randn(1, 8, 17, requires_grad=True)
    labels = torch.tensor([[-100, -100, -100, -100, -100, 6, 7, 8]])
    full_loss = grounding.causal_response_loss(logits, labels)
    full_gradient = torch.autograd.grad(full_loss, logits, retain_graph=True)[0]
    window_loss = grounding.causal_response_loss(logits[:, -4:, :], labels)
    window_gradient = torch.autograd.grad(window_loss, logits)[0]
    assert torch.allclose(full_loss, window_loss)
    assert torch.equal(full_gradient, window_gradient)
    assert not torch.count_nonzero(full_gradient[:, :4, :]).item()
    assert torch.count_nonzero(full_gradient[:, 4:7, :]).item()


def test_cli_missing_review_does_not_start_or_create_output(tmp_path, capsys):
    code = grounding.main(
        [
            "--plan",
            str(tmp_path / "missing-plan"),
            "--plan-sha256",
            grounding.PLAN_SHA256,
            "--dataset-manifest",
            str(tmp_path / "dataset.json"),
            "--dataset-sha256",
            "a" * 64,
            "--review-receipt",
            str(tmp_path / "review.json"),
            "--cache-directory",
            str(tmp_path / "cache"),
            "--output-directory",
            str(tmp_path / "output"),
        ]
    )
    assert code == 1
    assert not (tmp_path / "output").exists()
    assert "Grounding trainer refused" in capsys.readouterr().err


def test_native_eval_runtime_failure_preserves_failed_row(tmp_path):
    sample = dataset(tmp_path)["samples"][1]

    class Backend:
        def generate(self, _encoded, *, parent):
            raise RuntimeError("native CUDA failure")

    with pytest.raises(RuntimeError, match="native CUDA failure"):
        grounding.evaluate_rows(
            Backend(),
            [sample],
            {sample["sample_id"]: {}},
            parent=True,
            artifact_directory=tmp_path / "parent-rows",
        )
    failed = json.loads((tmp_path / "parent-rows" / f"{sample['sample_id']}.json").read_text())
    assert failed["native_generation_completed"] is False
    assert failed["failure_type"] == "RuntimeError"


@pytest.mark.parametrize("field,value", [("token_cap_reached", True), ("elapsed_s", 30.1)])
def test_bounded_eval_no_repair_of_late_or_truncated_raw(tmp_path, field, value, capsys):
    sample = dataset(tmp_path)["samples"][1]

    class Backend:
        def generate(self, _encoded, *, parent):
            return {
                "raw_output": json.dumps(target()),
                "output_token_ids": [1, 2],
                "generated_tokens": 2,
                "token_cap_reached": False,
                "elapsed_s": 0.1,
                field: value,
            }

    result = grounding.evaluate_rows(Backend(), [sample], {sample["sample_id"]: {}}, parent=True)
    assert result["strict_reply_count"] == 0
    assert result["samples"][0]["raw_output"] == json.dumps(target())
    assert "token cap or completed after" in result["samples"][0]["failure"]


@pytest.mark.parametrize("suffix", ['"extra":1e999', '"extra":{"nested":[-1e999]}'])
def test_float_overflow_raw_rejected_without_polluting_report(tmp_path, suffix, capsys):
    sample = dataset(tmp_path)["samples"][1]
    raw = json.dumps(target())[:-1] + "," + suffix + "}"

    class Backend:
        def generate(self, _encoded, *, parent):
            return {
                "raw_output": raw,
                "output_token_ids": [1, 2],
                "generated_tokens": 2,
                "token_cap_reached": False,
                "elapsed_s": 0.1,
            }

    result = grounding.evaluate_rows(
        Backend(),
        [sample],
        {sample["sample_id"]: {}},
        parent=True,
        artifact_directory=tmp_path / "rows",
    )
    assert result["strict_reply_count"] == 0
    assert result["samples"][0]["parsed_output"] is None
    assert result["samples"][0]["raw_output"] == raw
    path = tmp_path / "evidence.json"
    grounding._write_json(path, result)
    assert grounding.strict_factual_json(path.read_bytes())["samples"][0]["raw_output"] == raw


def test_json_writers_refuse_nonfinite_before_creating_output(tmp_path):
    value = {"nested": [float("inf")]}
    with pytest.raises(ValueError):
        grounding.canonical_bytes(value)
    path = tmp_path / "no-invalid-json.json"
    with pytest.raises(ValueError):
        grounding._write_json(path, value)
    assert not path.exists()

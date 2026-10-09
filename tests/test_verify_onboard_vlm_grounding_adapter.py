from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_onboard_vlm_grounding_adapter as verification

training = verification.training


def tiny_adapter():
    import peft
    import torch
    from transformers import Qwen3VLConfig, Qwen3VLForConditionalGeneration

    torch.manual_seed(741)
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
    base = Qwen3VLForConditionalGeneration(config).to(torch.bfloat16)
    base.requires_grad_(False)
    targets = [name for name, _ in base.named_modules() if training.LANGUAGE_TARGET.fullmatch(name)]
    assert len(targets) == 4
    return peft.get_peft_model(
        base,
        peft.LoraConfig(
            task_type="CAUSAL_LM",
            r=4,
            lora_alpha=8,
            lora_dropout=0.0,
            target_modules=targets,
            bias="none",
        ),
    )


@pytest.fixture
def adapter_state():
    import peft
    import torch

    model = tiny_adapter()
    with torch.no_grad():
        for parameter in model.parameters():
            if parameter.requires_grad:
                values = torch.linspace(0.1234567, 0.3456789, parameter.numel())
                parameter.copy_(values.reshape(parameter.shape))
    audit = training.audit_trainable_parameters(model)
    state = {
        key: value.detach().cpu().clone()
        for key, value in peft.get_peft_model_state_dict(model).items()
    }
    digest = training.parameter_digest(model, trainable=True)
    assert verification.saved_parameter_digest(state, audit, rank=4) == digest
    return model, state, audit, digest


def test_real_peft_second_bf16_adapter_loses_saved_fp32_precision(tmp_path, adapter_state):
    import peft
    import torch
    from safetensors.torch import load_file

    model, original_state, audit, digest = adapter_state
    model.save_pretrained(str(tmp_path / "adapter"))
    saved = load_file(str(tmp_path / "adapter" / "adapter_model.safetensors"))
    assert verification.saved_parameter_digest(saved, audit, rank=4) == digest
    model.load_adapter(str(tmp_path / "adapter"), adapter_name="sealed_reload", is_trainable=False)
    reloaded = peft.get_peft_model_state_dict(model, adapter_name="sealed_reload")
    assert set(reloaded) == set(original_state)
    assert all(value.dtype == torch.float32 for value in reloaded.values())
    assert any(not torch.equal(value, original_state[key]) for key, value in reloaded.items())
    assert all(
        torch.equal(value.cpu(), original_state[key].to(torch.bfloat16).float())
        for key, value in reloaded.items()
    )
    assert verification.saved_parameter_digest(reloaded, audit, rank=4) != digest
    assert any("sealed_reload" in name for name, _ in model.named_parameters())


def test_fresh_fp32_default_reload_exact_saved_bytes_and_base_unchanged(adapter_state):
    import peft
    import torch

    _trained, state, audit, digest = adapter_state
    fresh = tiny_adapter()
    before = training.parameter_digest(fresh, trainable=False)
    check = verification.load_saved_exactly(fresh, state, audit, digest, rank=4)
    assert check["exact_tensor_equality"] is True
    assert check["dtype_conversion_performed"] is False
    assert check["requires_grad_used_for_identity"] is False
    assert check["saved_parameter_sha256"] == check["loaded_parameter_sha256"] == digest
    assert check["lora_missing_keys"] == check["unexpected_keys"] == []
    assert len(check["tensor_checks"]) == len(audit["trainable_parameters"])
    assert all(item["loaded_bytes_equal_saved"] for item in check["tensor_checks"])
    assert all(item["dtype"] == "torch.float32" for item in check["tensor_checks"])
    fresh.requires_grad_(False)
    assert not any(parameter.requires_grad for parameter in fresh.parameters())
    frozen_loaded = peft.get_peft_model_state_dict(fresh)
    assert verification.saved_parameter_digest(frozen_loaded, audit, rank=4) == digest
    assert all(torch.equal(frozen_loaded[key], state[key]) for key in state)
    assert before == training.parameter_digest(fresh, trainable=False)


@pytest.mark.parametrize("mutation", ["missing", "extra", "dtype", "shape", "nonfinite"])
def test_bad_saved_tensor_rejected_without_repair(adapter_state, mutation):
    import torch

    _model, original, audit, _digest = adapter_state
    state = {key: value.clone() for key, value in original.items()}
    key = next(iter(state))
    if mutation == "missing":
        state.pop(key)
    elif mutation == "extra":
        state["base_model.model.visual.q_proj.lora_A.weight"] = torch.ones(4, 16)
    elif mutation == "dtype":
        state[key] = state[key].bfloat16()
    elif mutation == "shape":
        state[key] = state[key][:3]
    else:
        state[key][0, 0] = float("inf")
    with pytest.raises(ValueError):
        verification.saved_parameter_digest(state, audit, rank=4)


@pytest.mark.parametrize("mutation", ["duplicate", "visual", "count", "empty"])
def test_bad_trainable_audit_rejected(adapter_state, mutation):
    _model, state, original_audit, _digest = adapter_state
    audit = copy.deepcopy(original_audit)
    if mutation == "duplicate":
        audit["trainable_parameters"].append(audit["trainable_parameters"][0])
    elif mutation == "visual":
        audit["trainable_parameters"][0]["name"] = (
            "base_model.model.visual.q_proj.lora_A.default.weight"
        )
    elif mutation == "count":
        audit["trainable_parameter_count"] += 1
    else:
        audit["trainable_parameters"] = []
    with pytest.raises(ValueError):
        verification.saved_parameter_digest(state, audit, rank=4)


def test_saved_digest_mismatch_rejected_before_live_tensor_mutation(adapter_state):
    _model, state, audit, _digest = adapter_state
    fresh = tiny_adapter()
    before = training.parameter_digest(fresh, trainable=True)
    with pytest.raises(ValueError, match="actual trained"):
        verification.load_saved_exactly(fresh, state, audit, "a" * 64, rank=4)
    assert training.parameter_digest(fresh, trainable=True) == before


def test_original_parameter_order_is_part_of_the_trained_digest(adapter_state):
    _model, state, original, digest = adapter_state
    audit = copy.deepcopy(original)
    audit["trainable_parameters"].reverse()
    assert verification.saved_parameter_digest(state, audit, rank=4) != digest


def test_actual_fp32_byte_change_not_hidden_by_metadata(adapter_state):
    _model, state, audit, digest = adapter_state
    state[next(iter(state))][0, 0] += 0.001
    assert verification.saved_parameter_digest(state, audit, rank=4) != digest


def test_bf16_destination_rejected_instead_of_cast_repair(adapter_state):
    import torch

    _model, state, audit, digest = adapter_state
    fresh = tiny_adapter().to(torch.bfloat16)
    with pytest.raises(ValueError, match="already have"):
        verification.load_saved_exactly(fresh, state, audit, digest, rank=4)


def original_report():
    return {
        "contract": training.CONTRACT,
        "model_id": training.MODEL_ID,
        "revision": training.REVISION,
        "native_exit_code": 1,
        "failure": verification.ORIGINAL_FAILURE,
        "vlm_training_performed": True,
        "base_parameter_bytes_unchanged": True,
        "base_parameter_sha256_before": "a" * 64,
        "base_parameter_sha256_after": "a" * 64,
        **{
            name: False
            for name in (
                "candidate_promoted",
                "final_release_allowed",
                "transport_created",
                "execution_authority",
                "hil_real_evidence",
                "physical_performance_claimed",
                "outdoor_gnss_evidence",
            )
        },
    }


def test_original_native_one_stays_one():
    report = original_report()
    before = copy.deepcopy(report)
    verification.validate_original(report)
    assert report == before
    assert report["native_exit_code"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("native_exit_code", True),
        ("native_exit_code", 0),
        ("failure", "some other failure"),
        ("vlm_training_performed", False),
        ("base_parameter_sha256_after", "b" * 64),
        ("final_release_allowed", True),
        ("candidate_promoted", True),
        ("execution_authority", True),
    ],
)
def test_not_the_exact_failed_reload_gate_refused(field, value):
    report = original_report()
    report[field] = value
    with pytest.raises(ValueError):
        verification.validate_original(report)


@pytest.mark.parametrize(
    "payload", [b'{"a":1e999}', b'{"a":{"v":[-1e999]}}', b'{"a":1,"a":2}', b"[]"]
)
def test_bound_json_nonfinite_duplicate_nonobject_rejected(tmp_path, payload):
    path = tmp_path / "report.json"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        verification.read_bound_json(path, hashlib.sha256(payload).hexdigest())


def test_bound_json_expected_sha_not_current_self_attestation(tmp_path):
    path = tmp_path / "report.json"
    path.write_bytes(b'{"old":true}')
    pinned = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(b'{"changed":true}')
    with pytest.raises(ValueError, match="SHA"):
        verification.read_bound_json(path, pinned)


def test_locate_rejects_duplicate_identity():
    record = {"sha256": "a" * 64, "path": "D:/source/report.json"}
    with pytest.raises(ValueError, match="exactly one"):
        verification.locate([record, record], "a" * 64, "report.json")


def test_existing_output_refused_before_any_input_read_or_model_load(tmp_path):
    output = tmp_path / "preserved"
    output.mkdir()
    original = output / "evidence.json"
    original.write_bytes(b"previous evidence")
    args = SimpleNamespace(
        output_directory=output,
        cache_directory=tmp_path / "cache",
        training_report=tmp_path / "missing.json",
        training_report_sha256="a" * 64,
    )
    with pytest.raises(FileExistsError, match="never overwrites"):
        verification.run(args, backend_factory=lambda *_args: pytest.fail("must not load"))
    assert original.read_bytes() == b"previous evidence"


def test_adapter_config_identity_unchanged_and_no_visual_target_repair():
    config = {
        "base_model_name_or_path": training.MODEL_ID,
        "revision": training.REVISION,
        "peft_type": "LORA",
        "task_type": "CAUSAL_LM",
        "r": 4,
        "lora_alpha": 8,
        "lora_dropout": 0.0,
        "bias": "none",
        "modules_to_save": None,
        "use_dora": False,
        "use_rslora": False,
        "target_modules": ["q_proj", "v_proj"],
    }
    before = copy.deepcopy(config)
    verification.verify_config(config, training.TrainingBudget())
    assert config == before
    config["target_modules"] = ["qkv"]
    with pytest.raises(ValueError, match="target modules"):
        verification.verify_config(config, training.TrainingBudget())


def test_actual_saved_checkpoint_reconstructs_original_trained_digest_without_gpu():
    from safetensors.torch import load_file

    directory = (
        Path(verification.__file__).parents[1]
        / "artifacts/models/onboard-vlm-grounding-lora-dev-20261007-r1"
    )
    report_path = directory / "report.json"
    if not report_path.is_file():
        pytest.skip("Original development artifacts not present; tests never download or train")
    report = verification.read_bound_json(
        report_path, "6cfb56135f1f2d489565e2eedc8f3e31467b1f073cc6bee15018169f59916871"
    )
    verification.validate_original(report)
    weights = directory / "adapter/adapter_model.safetensors"
    state = load_file(str(weights), device="cpu")
    digest = verification.saved_parameter_digest(state, report["trainable_audit"], rank=4)
    assert digest == report["adapter_parameter_sha256_after"]
    assert len(state) == 112
    assert report["native_exit_code"] == 1


def test_missing_original_report_cli_no_output(tmp_path, capsys):
    output = tmp_path / "output"
    code = verification.main(
        [
            "--training-report",
            str(tmp_path / "missing.json"),
            "--training-report-sha256",
            "a" * 64,
            "--cache-directory",
            str(tmp_path / "cache"),
            "--output-directory",
            str(output),
        ]
    )
    assert code == 1
    assert not output.exists()
    assert "Independent reload check refused" in capsys.readouterr().err

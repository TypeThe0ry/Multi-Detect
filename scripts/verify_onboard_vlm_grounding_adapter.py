"""Independent recovery check for the saved factual adapter, never retraining.

The failed training report remains failed and immutable. A fresh pinned BF16
base receives the exact saved FP32 tensors in a precreated FP32 default adapter.
No second-adapter BF16 construction, dtype repair, optimizer or transport is used.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scripts import train_onboard_vlm_grounding as training

probe = training.probe
CONTRACT = "multidetect.onboard-vlm-grounding-adapter-reload-check-v1"
ORIGINAL_FAILURE = "Actual offline loaded adapter tensor bytes differ from trained adapter"
ADAPTER_PARAMETER = re.compile(
    r"base_model\.model\.model\.language_model\.layers\.\d+\.self_attn\."
    r"(q_proj|v_proj)\.lora_(A|B)\.default\.weight"
)


def read_bound_json(path: Path, expected_sha256: str) -> dict:
    probe.require_sha256(expected_sha256)
    payload = path.read_bytes()
    if len(payload) > 32 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("Bound JSON size or expected SHA mismatches")
    parsed = training.strict_factual_json(payload)
    if not isinstance(parsed, dict):
        raise ValueError("A bound JSON object is required")
    return parsed


def locate(records: list[dict], expected_sha256: str, basename: str) -> dict:
    matches = [
        item
        for item in records
        if item["sha256"] == expected_sha256 and Path(item["path"]).name == basename
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one bound {basename}")
    return matches[0]


def validate_original(original: dict) -> None:
    if (
        original.get("contract") != training.CONTRACT
        or original.get("model_id") != training.MODEL_ID
        or original.get("revision") != training.REVISION
        or type(original.get("native_exit_code")) is not int
        or original["native_exit_code"] != 1
        or original.get("failure") != ORIGINAL_FAILURE
        or original.get("vlm_training_performed") is not True
        or original.get("base_parameter_bytes_unchanged") is not True
        or original.get("base_parameter_sha256_before")
        != original.get("base_parameter_sha256_after")
    ):
        raise ValueError("Only the exact known failed reload gate can be independently checked")
    if any(
        original.get(name) is not False
        for name in (
            "candidate_promoted",
            "final_release_allowed",
            "transport_created",
            "execution_authority",
            "hil_real_evidence",
            "physical_performance_claimed",
            "outdoor_gnss_evidence",
        )
    ):
        raise ValueError("The original development evidence overstates authority or acceptance")


def original_records(original: dict) -> list[dict]:
    return [
        *original["source_files"],
        *original["runtime_files"],
        *original["model_provenance"]["files"],
        *original["adapter_files"],
        *original["output_artifact_files"],
    ]


def saved_parameter_digest(state: dict, audit: dict, *, rank: int) -> str:
    """Reconstruct original parameter names/order, independent of requires_grad."""
    import torch

    entries = audit["trainable_parameters"]
    if not entries or audit["trainable_parameter_count"] <= 0:
        raise ValueError("An empty trainable-parameter hash cannot prove an adapter")
    expected_keys = set()
    names = set()
    digest = hashlib.sha256()
    numel = 0
    for item in entries:
        name = item["name"]
        match = ADAPTER_PARAMETER.fullmatch(name)
        if match is None or name in names:
            raise ValueError("Original trainable audit is not unique language-only q/v LoRA")
        names.add(name)
        key = name.replace(".default.", ".")
        expected_keys.add(key)
        if key not in state:
            raise ValueError("The saved checkpoint is missing an audited adapter tensor")
        tensor = state[key]
        if tensor.ndim != 2 or tensor.shape[0 if match[2] == "A" else 1] != rank:
            raise ValueError("Saved LoRA tensor rank/shape differs from the declared adapter")
        if tensor.dtype != torch.float32 or item["dtype"] != "torch.float32":
            raise ValueError("FP32 adapter tensors must not be repaired from another dtype")
        if tensor.numel() != item["numel"] or not torch.isfinite(tensor).all().item():
            raise ValueError("Saved LoRA tensor size or finite-value check failed")
        numel += tensor.numel()
        digest.update(training.canonical_bytes([name, list(tensor.shape), str(tensor.dtype)]))
        digest.update(tensor.detach().cpu().contiguous().view(torch.uint8).numpy().tobytes())
    if set(state) != expected_keys or numel != audit["trainable_parameter_count"]:
        raise ValueError("Saved checkpoint has extra tensors or the parameter count differs")
    return digest.hexdigest()


def load_saved_exactly(model, state: dict, audit: dict, expected_digest: str, *, rank: int) -> dict:
    import peft
    import torch
    from peft.utils.save_and_load import set_peft_model_state_dict

    actual = saved_parameter_digest(state, audit, rank=rank)
    if actual != expected_digest:
        raise ValueError("Saved tensor bytes differ from the actual trained parameter digest")
    current = dict(model.named_parameters())
    parameter_names = {item["name"] for item in audit["trainable_parameters"]}
    live_lora = {name for name in current if ".lora_A." in name or ".lora_B." in name}
    if live_lora != parameter_names:
        raise ValueError("The fresh model has unexpected or missing adapter parameters")
    for name in parameter_names:
        source = state[name.replace(".default.", ".")]
        destination = current[name]
        if destination.shape != source.shape or destination.dtype != torch.float32:
            raise ValueError("Default adapter must already have the exact shape and FP32 dtype")
    result = set_peft_model_state_dict(
        model, state, adapter_name="default", low_cpu_mem_usage=False
    )
    missing = list(result.missing_keys)
    unexpected = list(result.unexpected_keys)
    expected_missing = set(model.state_dict()) - parameter_names
    if set(missing) != expected_missing or unexpected:
        raise ValueError("PEFT reported unexpected missing/unexpected checkpoint keys")
    loaded = peft.get_peft_model_state_dict(model, adapter_name="default")
    loaded_digest = saved_parameter_digest(loaded, audit, rank=rank)
    if loaded_digest != actual:
        raise ValueError("Actual loaded tensor bytes differ from the saved FP32 adapter")
    if any(not torch.equal(loaded[key].cpu(), state[key].cpu()) for key in state):
        raise ValueError("A supposedly equal digest has a tensor-level mismatch")
    return {
        "saved_parameter_sha256": actual,
        "loaded_parameter_sha256": loaded_digest,
        "exact_tensor_equality": True,
        "tensor_count": len(state),
        "tensor_checks": [
            {
                "original_parameter_name": item["name"],
                "saved_key": item["name"].replace(".default.", "."),
                "shape": list(state[item["name"].replace(".default.", ".")].shape),
                "dtype": str(state[item["name"].replace(".default.", ".")].dtype),
                "numel": item["numel"],
                "loaded_bytes_equal_saved": True,
            }
            for item in audit["trainable_parameters"]
        ],
        "lora_missing_keys": [],
        "unexpected_keys": unexpected,
        "expected_frozen_base_missing_key_count": len(missing),
        "requires_grad_used_for_identity": False,
        "dtype_conversion_performed": False,
    }


def verify_config(config: dict, budget: training.TrainingBudget) -> None:
    expected = {
        "base_model_name_or_path": training.MODEL_ID,
        "revision": training.REVISION,
        "peft_type": "LORA",
        "task_type": "CAUSAL_LM",
        "r": budget.rank,
        "lora_alpha": budget.alpha,
        "lora_dropout": 0.0,
        "bias": "none",
        "modules_to_save": None,
        "use_dora": False,
        "use_rslora": False,
    }
    if any(config.get(name) != value for name, value in expected.items()):
        raise ValueError("Original adapter config differs from the frozen LoRA experiment")
    if set(config.get("target_modules", [])) != {"q_proj", "v_proj"}:
        raise ValueError("The original PEFT suffix-compressed target modules changed")


def run(args, *, backend_factory=training.GroundingBackend) -> int:
    output = probe.require_d_storage(args.output_directory)
    cache = probe.require_d_storage(args.cache_directory)
    if output.exists():
        raise FileExistsError("Reload verification never overwrites previous evidence")
    original = read_bound_json(args.training_report, args.training_report_sha256)
    validate_original(original)
    old_records = original_records(original)
    source_records = [probe.file_record(Path(__file__)), probe.file_record(args.training_report)]
    if source_records[1]["sha256"] != args.training_report_sha256:
        raise ValueError("Original training report changed during admission")
    probe.verify_records(old_records + source_records)
    source_records.append(probe.file_record(Path(training.__file__)))
    plan_record = locate(
        original["source_files"],
        original["training_plan_sha256"],
        "onboard-vlm-grounding-lora-dev-v1.json",
    )
    plan = training.load_training_plan(Path(plan_record["path"]), original["training_plan_sha256"])
    budget = training.budget_from_plan(plan)
    if original["budget"] != training.asdict(budget):
        raise ValueError("Original training budget differs from the sealed experiment")
    dataset_record = locate(
        original["source_files"], original["dataset_sha256"], "manifest.pending-review.json"
    )
    review_record = locate(
        original["source_files"], original["review_receipt_sha256"], "review-receipt.json"
    )
    from scripts.build_onboard_vlm_grounding_dataset import validate_grounding_dataset

    dataset_path = Path(dataset_record["path"])
    document = validate_grounding_dataset(
        dataset_path,
        original["dataset_sha256"],
        review_receipt=Path(review_record["path"]),
        require_review=True,
    )
    splits = training.admit_samples(document, budget)
    if original["declared_split_sample_ids"] != {
        split: [sample["sample_id"] for sample in rows] for split, rows in splits.items()
    }:
        raise ValueError("Original training split identities differ from reviewed data")
    sample = splits["val"][0]
    candidates = [
        row
        for row in original["candidate_evaluation"]["samples"]
        if row["sample_id"] == sample["sample_id"] and row["split"] == "val"
    ]
    if len(candidates) != 1:
        raise ValueError(
            "The predeclared first validation sample has no unique candidate generation"
        )
    candidate = candidates[0]
    config_record = next(
        item
        for item in original["adapter_files"]
        if Path(item["path"]).name == "adapter_config.json"
    )
    weights_record = next(
        item
        for item in original["adapter_files"]
        if Path(item["path"]).name == "adapter_model.safetensors"
    )
    config = read_bound_json(Path(config_record["path"]), config_record["sha256"])
    verify_config(config, budget)
    base_record = next(
        item
        for item in original["model_provenance"]["files"]
        if item["sha256"] == plan["base_weights_sha256"]
    )
    snapshot = Path(base_record["path"]).parent
    probe.configure_d_cache(cache)
    from safetensors.torch import load_file

    state = load_file(weights_record["path"], device="cpu")
    saved_digest = saved_parameter_digest(state, original["trainable_audit"], rank=budget.rank)
    if saved_digest != original["adapter_parameter_sha256_after"]:
        raise ValueError("Actual saved tensor bytes do not reproduce the original trained digest")
    probe.verify_records(old_records + source_records)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "original_training_report_sha256": args.training_report_sha256,
        "original_training_native_exit_code": original["native_exit_code"],
        "original_training_reload_gate_passed": False,
        "original_training_failure": original["failure"],
        "original_optimizer_steps_completed": original["optimizer_steps_completed"],
        "optimizer_steps_performed_by_this_check": 0,
        "training_performed_by_this_check": False,
        "fresh_explicit_base_snapshot": str(snapshot),
        "model_id": training.MODEL_ID,
        "revision": training.REVISION,
        "saved_adapter_sha256": weights_record["sha256"],
        "saved_parameter_sha256": saved_digest,
        "predeclared_representative_split": "val",
        "sample_id": sample["sample_id"],
        "original_bindings": old_records,
        "source_files": source_records,
        "candidate_promoted": False,
        "final_release_allowed": False,
        "transport_created": False,
        "execution_authority": False,
        "hil_real_evidence": False,
        "physical_performance_claimed": False,
        "jetson_evidence": False,
        "outdoor_gnss_evidence": False,
        "native_exit_code": 1,
    }
    try:
        backend = backend_factory(snapshot, budget)
        report["runtime_files"] = backend.runtime_records()
        report["fresh_base_sha256"] = backend.base_digest_initial
        if backend.base_digest_initial != original["base_parameter_sha256_before"]:
            raise ValueError("Fresh actual base tensor bytes differ from the trained base identity")
        report["load_check"] = load_saved_exactly(
            backend.model,
            state,
            original["trainable_audit"],
            original["adapter_parameter_sha256_after"],
            rank=budget.rank,
        )
        backend.model.requires_grad_(False)
        if any(parameter.requires_grad for parameter in backend.model.parameters()):
            raise ValueError("Reload verification must not leave trainable parameters")
        encoded = backend.encode(sample, dataset_path.parent)
        actual_encoding = {
            name: encoded[name] for name in original["encoded_samples"][sample["sample_id"]]
        }
        if actual_encoding != original["encoded_samples"][sample["sample_id"]]:
            raise ValueError("Actual processor tensors differ from the recorded candidate input")
        generated = backend.generate(encoded, parent=False)
        report["generation"] = generated
        if generated["output_token_ids"] != candidate["output_token_ids"] or (
            generated["raw_output"] != candidate["raw_output"]
        ):
            raise ValueError("Actual saved adapter generation differs from the original candidate")
        report["generation_token_ids_and_raw_equal"] = True
        report["base_sha256_after"] = training.parameter_digest(backend.model, trainable=False)
        if report["base_sha256_after"] != report["fresh_base_sha256"]:
            raise ValueError("Adapter loading/inference changed actual frozen base tensor bytes")
        report["base_parameter_bytes_unchanged"] = True
        probe.verify_records(old_records + source_records + report["runtime_files"])
        report["source_bytes_reverified"] = True
        report["native_exit_code"] = 0
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        report["failure"] = str(exc)
    training._write_json(output / "report.json", report)
    return report["native_exit_code"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-report", type=Path, required=True)
    parser.add_argument("--training-report-sha256", required=True)
    parser.add_argument("--cache-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    try:
        return run(parser.parse_args(argv))
    except Exception as exc:
        print(f"Independent reload check refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

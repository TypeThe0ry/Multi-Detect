"""Train-only visual grounding increment, warm-starting the sealed r1 adapter.

No flight actions, navigation labels, model promotion or simulator commands.
The old failed run and its source stay immutable. Exact FP32 adapter loading
uses the independently verified recovery implementation, not load_adapter.
"""

from __future__ import annotations

import argparse
import gc
import json
import random
import sys
import time
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scripts import train_onboard_vlm_grounding as core
from scripts import verify_onboard_vlm_grounding_adapter as recovery

CONTRACT = "multidetect.onboard-vlm-grounding-increment-training-v1"
PLAN_CONTRACT = "multidetect.onboard-vlm-grounding-increment-plan-v1"
PARENT_REPORT_SHA256 = "6cfb56135f1f2d489565e2eedc8f3e31467b1f073cc6bee15018169f59916871"
PARENT_ADAPTER_SHA256 = "e31d71f1b0b89136574728603ef611fbd77bd6d0edb6d4445c803987640120bb"
RECOVERY_REPORT_SHA256 = "d790559bef872510f0795fe927b7e06c363a2dd0ca7a90174da14a64f6ca8973"
FALSE_FLAGS = (
    "candidate_promoted",
    "final_release_allowed",
    "transport_created",
    "execution_authority",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "flight_action_training_performed",
    "checkpoint_selection_performed",
)


def bound_json(path: Path, expected: str) -> dict:
    return recovery.read_bound_json(path, expected)


def load_plan(path: Path, expected: str) -> tuple[dict, core.TrainingBudget]:
    plan = bound_json(path, expected)
    if (
        plan.get("contract") != PLAN_CONTRACT
        or plan.get("model_id") != core.MODEL_ID
        or plan.get("revision") != core.REVISION
        or plan.get("parent_report_sha256") != PARENT_REPORT_SHA256
        or plan.get("parent_adapter_sha256") != PARENT_ADAPTER_SHA256
        or plan.get("recovery_report_sha256") != RECOVERY_REPORT_SHA256
        or plan.get("optimizer_reads_only_train_split") is not True
        or plan.get("holdout_inputs_unchanged") is not True
        or plan.get("fixed_final_checkpoint") is not True
        or any(plan.get(name) is not False for name in FALSE_FLAGS)
    ):
        raise ValueError("The bound increment plan changes provenance or evaluation authority")
    budget = core.TrainingBudget(**plan["budget"])
    budget.validate()
    if budget.epochs != 1 or budget.learning_rate != 0.00002 or budget.seed != 20261019:
        raise ValueError("Only the fixed single-epoch low-rate increment is admitted")
    if budget.rank != 4 or budget.alpha != 8 or not budget.gradient_checkpointing:
        raise ValueError("The increment must retain the actual parent adapter architecture")
    return plan, budget


def warm_start(backend, original: dict, adapter_path: Path) -> dict:
    """Load the actual parent tensors into already-FP32 default parameters."""
    from safetensors.torch import load_file

    weights = adapter_path / "adapter_model.safetensors"
    if core.probe.file_record(weights)["sha256"] != PARENT_ADAPTER_SHA256:
        raise ValueError("Warm-start weights differ from the sealed parent")
    config = recovery.read_bound_json(
        adapter_path / "adapter_config.json",
        next(
            record["sha256"]
            for record in original["adapter_files"]
            if Path(record["path"]).name == "adapter_config.json"
        ),
    )
    recovery.verify_config(config, backend.budget)
    if backend.base_digest_initial != original["base_parameter_sha256_after"]:
        raise ValueError("Fresh pinned base/vision bytes differ from the trained parent")
    if backend.trainable_audit != original["trainable_audit"]:
        raise ValueError("Live adapter parameter inventory differs from the parent")
    state = load_file(str(weights), device="cpu")
    return recovery.load_saved_exactly(
        backend.model,
        state,
        original["trainable_audit"],
        original["adapter_parameter_sha256_after"],
        rank=backend.budget.rank,
    )


def input_inventory(encoded: dict) -> dict:
    fields = (
        "prefix_tokens",
        "total_tokens",
        "response_tokens",
        "supervised_tokens",
        "preprocessing",
        "actual_image_grid_thw",
        "actual_image_pixels",
        "input_tensors",
        "response_labels_sha256",
    )
    return {identifier: {key: row[key] for key in fields} for identifier, row in encoded.items()}


def quality_comparison(parent: dict, candidate: dict) -> dict:
    """Predeclared class-specific gates; never repair outputs or tune a threshold."""
    if [row["sample_id"] for row in parent["samples"]] != [
        row["sample_id"] for row in candidate["samples"]
    ] or [row["model_input_sha256"] for row in parent["samples"]] != [
        row["model_input_sha256"] for row in candidate["samples"]
    ]:
        raise ValueError("Parent and candidate did not read the identical ordered holdout")
    gates = {
        "strict_reply_count_nonregression": candidate["strict_reply_count"]
        >= parent["strict_reply_count"],
        "exact_match_count_nonregression": candidate["exact_match_count"]
        >= parent["exact_match_count"],
    }
    for split in ("val", "test"):
        before = parent["metrics"]["per_split_visibility"][split]
        after = candidate["metrics"]["per_split_visibility"][split]
        gates[f"{split}_visible_nonregression"] = after["tp"] >= before["tp"]
        gates[f"{split}_not_confirmed_nonregression"] = after["tn"] >= before["tn"]
    gates["at_least_one_extra_correct_zero_pixel_holdout"] = (
        candidate["metrics"]["visibility_confusion"]["tn"]
        > parent["metrics"]["visibility_confusion"]["tn"]
    )
    gates["all_navigation_fields_remain_unknown"] = (
        candidate["metrics"]["unknown_navigation_fact_count"] == candidate["count"]
    )
    return {
        "gates": gates,
        "quality_passed": all(gates.values()),
        "holdout_status": "known_development_render_regression_not_new_blind_benchmark",
        "action_learning_claimed": False,
        "identity_recovery_claimed": False,
        "candidate_promoted": False,
    }


def train_epoch(
    backend, samples: list[dict], encoded: dict, output: Path, *, progress: dict | None = None
) -> list[dict]:
    if any(sample["split"] != "train" for sample in samples):
        raise ValueError("A holdout cannot enter the optimizer")
    if not samples or len(samples) > backend.budget.max_optimizer_steps:
        raise ValueError("The whole train epoch does not fit the step budget")
    parameters = [p for p in backend.model.parameters() if p.requires_grad]
    progress = {} if progress is None else progress
    progress.update(
        {
            "optimizer_steps_attempted": 0,
            "optimizer_native_calls_returned": 0,
            "optimizer_steps_completed": 0,
            "optimizer_cuda_sync_completed_count": 0,
            "optimizer_update_uncertain": False,
        }
    )
    optimizer = backend.torch.optim.AdamW(
        parameters, lr=backend.budget.learning_rate, weight_decay=0.0
    )
    shuffled = list(samples)
    random.Random(backend.budget.seed).shuffle(shuffled)
    entries = []
    with (output / "training-log.jsonl").open("x", encoding="utf-8") as log:
        for sample in shuffled:
            optimizer.zero_grad(set_to_none=True)
            backend.torch.cuda.reset_peak_memory_stats()
            started = time.perf_counter()
            loss = backend.loss(encoded[sample["sample_id"]])
            if not backend.torch.isfinite(loss).item():
                raise ValueError("Actual increment loss is non-finite")
            loss.backward()
            norm = backend.torch.nn.utils.clip_grad_norm_(parameters, 1.0, error_if_nonfinite=True)
            progress["optimizer_steps_attempted"] += 1
            progress["optimizer_update_uncertain"] = True
            optimizer.step()
            # A returned asynchronous call is not proof that CUDA completed it.
            progress["optimizer_native_calls_returned"] += 1
            backend.torch.cuda.synchronize()
            progress["optimizer_cuda_sync_completed_count"] += 1
            progress["optimizer_steps_completed"] += 1
            progress["vlm_training_performed"] = True
            progress["optimizer_update_uncertain"] = False
            entry = {
                "step": len(entries) + 1,
                "epoch": 1,
                "sample_id": sample["sample_id"],
                "split": "train",
                "loss": loss.detach().item(),
                "gradient_norm_before_clip": norm.item(),
                "elapsed_s": time.perf_counter() - started,
                "peak_allocated_bytes": backend.torch.cuda.max_memory_allocated(),
                "peak_reserved_bytes": backend.torch.cuda.max_memory_reserved(),
            }
            entries.append(entry)
            log.write(json.dumps(entry, sort_keys=True) + "\n")
            log.flush()
            print(json.dumps({"phase": "increment_optimizer", **entry}), flush=True)
    backend.model.zero_grad(set_to_none=True)
    return entries


def run(args) -> int:
    from scripts.build_onboard_vlm_grounding_increment_dataset import (
        validate_grounding_increment_dataset,
    )

    plan, budget = load_plan(args.plan, args.plan_sha256)
    original = bound_json(args.parent_report, PARENT_REPORT_SHA256)
    recovery.validate_original(original)
    verified = bound_json(args.recovery_report, RECOVERY_REPORT_SHA256)
    if (
        verified.get("native_exit_code") != 0
        or verified.get("base_parameter_bytes_unchanged") is not True
        or verified.get("optimizer_steps_performed_by_this_check") != 0
    ):
        raise ValueError("Exact parent adapter recovery was not independently successful")
    core.probe.verify_records(recovery.original_records(original))
    document = validate_grounding_increment_dataset(
        args.dataset_manifest,
        args.dataset_sha256,
        review_receipt=args.review_receipt,
        require_review=True,
    )
    if plan["dataset_sha256"] != args.dataset_sha256:
        raise ValueError("Plan and admitted dataset have different bytes")
    if core.probe.file_record(args.review_receipt)["sha256"] != plan["review_receipt_sha256"]:
        raise ValueError("Plan and independent dataset reviewer differ")
    splits = core.admit_samples(document, budget)
    if {name: len(rows) for name, rows in splits.items()} != plan["split_counts"]:
        raise ValueError("Plan and actual whole splits differ")
    cache = core.probe.require_d_storage(args.cache_directory)
    output = core.probe.require_d_storage(args.output_directory)
    if output.exists():
        raise FileExistsError("Never overwrite a prior increment or failed report")
    sources = [
        core.probe.file_record(path)
        for path in (
            Path(__file__),
            Path(core.__file__),
            Path(recovery.__file__),
            args.plan,
            args.dataset_manifest,
            args.review_receipt,
            args.parent_report,
            args.recovery_report,
            Path(__file__).with_name("build_onboard_vlm_grounding_increment_dataset.py"),
            Path(__file__).resolve().parents[1]
            / "tests/test_train_onboard_vlm_grounding_increment.py",
        )
    ]
    core.probe.verify_records(sources)
    core.probe.configure_d_cache(cache)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "contract": CONTRACT,
        "model_id": core.MODEL_ID,
        "revision": core.REVISION,
        "plan_sha256": args.plan_sha256,
        "dataset_sha256": args.dataset_sha256,
        "parent_adapter_sha256": PARENT_ADAPTER_SHA256,
        "parent_original_native_exit_code": 1,
        "parent_exact_reload_native_exit_code": 0,
        "budget": asdict(budget),
        "source_files": sources,
        "optimizer_steps_completed": 0,
        "vlm_training_performed": False,
        "native_exit_code": 1,
        **{name: False for name in FALSE_FLAGS},
    }
    core._write_json(output / "invocation.json", report)
    try:
        snapshot, provenance = core.probe.prepare_model_snapshot(
            core.MODEL_ID, core.REVISION, cache
        )
        report["model_provenance"] = provenance
        backend = core.GroundingBackend(snapshot, budget)
        report["runtime_files"] = backend.runtime_records()
        report["gpu"] = backend.torch.cuda.get_device_name(0)
        report["trainable_audit"] = backend.trainable_audit
        report["warm_start"] = warm_start(backend, original, args.parent_adapter)
        report["base_parameter_sha256_before"] = backend.base_digest_initial
        report["adapter_parameter_sha256_before"] = core.parameter_digest(
            backend.model, trainable=True
        )
        encoded = {
            sample["sample_id"]: backend.encode(sample, args.dataset_manifest.parent)
            for sample in document["samples"]
        }
        report["encoded_samples"] = input_inventory(encoded)
        holdout = splits["val"] + splits["test"]
        print(
            json.dumps({"phase": "warm_started_parent_evaluation", "count": len(holdout)}),
            flush=True,
        )
        parent = core.evaluate_rows(
            backend, holdout, encoded, parent=False, artifact_directory=output / "parent-rows"
        )
        parent["role"] = "actual_r1_adapter_enabled_before_increment"
        core._write_json(output / "parent-evaluation.json", parent)
        report["parent_evaluation"] = parent
        maximum = max(splits["train"], key=lambda row: encoded[row["sample_id"]]["total_tokens"])
        report["backward_preflight"] = backend.backward_preflight(encoded[maximum["sample_id"]])
        # Revalidate after the potentially long parent evaluation and backward
        # probe. A revoked/changed review must stop before optimizer creation.
        validate_grounding_increment_dataset(
            args.dataset_manifest,
            args.dataset_sha256,
            review_receipt=args.review_receipt,
            require_review=True,
        )
        current_plan, current_budget = load_plan(args.plan, args.plan_sha256)
        if current_plan != plan or current_budget != budget:
            raise ValueError("Bound training plan changed before optimizer admission")
        if core.probe.file_record(args.review_receipt)["sha256"] != plan["review_receipt_sha256"]:
            raise ValueError("Independent review bytes changed before optimizer admission")
        core.probe.verify_records(sources + provenance["files"] + report["runtime_files"])
        report["pre_optimizer_source_revalidation_passed"] = True
        report["optimization_rows"] = train_epoch(
            backend, splits["train"], encoded, output, progress=report
        )
        report["optimizer_steps_completed"] = len(report["optimization_rows"])
        report["vlm_training_performed"] = True
        adapter = output / "adapter"
        backend.model.save_pretrained(adapter, safe_serialization=True)
        report["adapter_files"] = [
            core.probe.file_record(path) for path in sorted(adapter.rglob("*")) if path.is_file()
        ]
        print(
            json.dumps({"phase": "increment_candidate_evaluation", "count": len(holdout)}),
            flush=True,
        )
        candidate = core.evaluate_rows(
            backend, holdout, encoded, parent=False, artifact_directory=output / "candidate-rows"
        )
        candidate["role"] = "actual_r2_adapter_enabled_after_increment"
        core._write_json(output / "candidate-evaluation.json", candidate)
        report["candidate_evaluation"] = candidate
        report["quality_comparison"] = quality_comparison(parent, candidate)
        report["base_parameter_sha256_after"] = core.parameter_digest(
            backend.model, trainable=False
        )
        digest = core.parameter_digest(backend.model, trainable=True)
        report["adapter_parameter_sha256_after"] = digest
        if report["base_parameter_sha256_after"] != report["base_parameter_sha256_before"]:
            raise ValueError("Frozen base/vision bytes changed during the increment")
        if digest == report["adapter_parameter_sha256_before"]:
            raise ValueError("Actual increment optimizer did not update adapter bytes")
        report["base_parameter_bytes_unchanged"] = True
        representative = splits["val"][0]
        recorded = next(
            row for row in candidate["samples"] if row["sample_id"] == representative["sample_id"]
        )
        # Free the trained model before constructing a fresh pinned base, not a
        # second BF16 adapter in the same model that would lose saved precision.
        del backend
        gc.collect()
        import torch
        from safetensors.torch import load_file

        torch.cuda.empty_cache()
        fresh = core.GroundingBackend(snapshot, budget)
        report["saved_adapter_reload"] = recovery.load_saved_exactly(
            fresh.model,
            load_file(str(adapter / "adapter_model.safetensors"), device="cpu"),
            report["trainable_audit"],
            digest,
            rank=budget.rank,
        )
        replay = fresh.generate(
            fresh.encode(representative, args.dataset_manifest.parent), parent=False
        )
        if (
            replay["output_token_ids"] != recorded["output_token_ids"]
            or replay["raw_output"] != recorded["raw_output"]
        ):
            raise ValueError("Fresh-base exact reload failed to reproduce the fixed val sample")
        if fresh.base_digest_initial != report["base_parameter_sha256_before"]:
            raise ValueError("Reload used different frozen base/vision bytes")
        report["saved_adapter_reload"].update(
            {
                "fresh_base": True,
                "generation_token_ids_equal": True,
                "raw_output_equal": True,
                "sample_id": representative["sample_id"],
                "generation": replay,
            }
        )
        validate_grounding_increment_dataset(
            args.dataset_manifest,
            args.dataset_sha256,
            review_receipt=args.review_receipt,
            require_review=True,
        )
        core.probe.verify_records(
            sources + provenance["files"] + report["runtime_files"] + report["adapter_files"]
        )
        report["native_exit_code"] = 0
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        report["failure"] = str(exc)
        log = output / "training-log.jsonl"
        if log.is_file():
            report["optimizer_log_rows_completed"] = len(
                log.read_text(encoding="utf-8").splitlines()
            )
        if report.get("optimizer_update_uncertain"):
            report["vlm_training_performed"] = (
                True if report["optimizer_steps_completed"] > 0 else None
            )
        print(
            json.dumps(
                {"phase": "failed", "failure_type": type(exc).__name__, "failure": str(exc)}
            ),
            flush=True,
        )
    report["output_artifact_files"] = [
        core.probe.file_record(path) for path in sorted(output.rglob("*")) if path.is_file()
    ]
    core._write_json(output / "report.json", report)
    return report["native_exit_code"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "plan",
        "dataset-manifest",
        "review-receipt",
        "parent-report",
        "recovery-report",
        "parent-adapter",
        "cache-directory",
        "output-directory",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--dataset-sha256", required=True)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

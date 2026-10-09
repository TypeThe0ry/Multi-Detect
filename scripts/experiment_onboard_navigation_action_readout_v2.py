"""Import-order-only v2 of the sealed candidate readout experiment.

The r1 failed before any feature/prediction/head update. Its receipt and all four
zero-spend counters are bound here; v2 shares the same family maximum800, not a
new800 allowance. The multimodal/features/head/statistical method is unchanged.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-action-readout-experiment-v2"
NEW_FILES = (
    "scripts/experiment_onboard_navigation_action_readout_v2.py",
    "tests/test_experiment_onboard_navigation_action_readout_v2.py",
)
R1_PINS = {
    "scripts/experiment_onboard_navigation_action_readout.py": (
        48027,
        "afe7cee154f3353e3005b7c6fa0893f8e655bbcc54040f37128cec418df81c35",
    ),
    "tests/test_experiment_onboard_navigation_action_readout.py": (
        46016,
        "ea34257f005348cba3ca9c0457a31bce315eb484d328d40ee8d19d4522b6aa31",
    ),
}
FAILURE_PINS = {
    "plan": (
        "configs/training/onboard-navigation-action-readout-20261008-r1.json",
        "70d8234fd108a467f223e31c4ec51e87bdfdae4b33b0679d24552774b65a498c",
    ),
    "report": (
        "artifacts/evaluation/onboard-navigation-action-readout-20261008-r1/report.json",
        "fdbd5292ee66440ef4bd91fce8ddb85bd75edc3edc85569933c50cfb4511cf86",
    ),
    "receipt": (
        "docs/evidence/onboard-navigation-action-readout-loader-failure-20261008-r1.json",
        "bf4fd371cf0f5afca95fa9ba2d59635aaeaab4fbd6b569424d3c2f7c202f6c7c",
    ),
}


def sealed_r1():
    for name, (size, digest) in R1_PINS.items():
        path = ROOT / name
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError("Sealed r1 source/test byte size differs")
        raw = path.read_bytes()
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Sealed r1 source/test SHA differs")
    spec = importlib.util.spec_from_file_location(
        "_sealed_readout_experiment_r1", ROOT / next(iter(R1_PINS))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_r1 = sealed_r1()
RULE = _r1.RULE
FALSE_CLAIMS = _r1.FALSE_CLAIMS
canonical = _r1.canonical
safe_path = _r1.safe_path
small_record = _r1.small_record
evaluation_api = _r1.evaluation_api
admit_runtime = _r1.admit_runtime
verify_runtime_modules = _r1.verify_runtime_modules
load_samples = _r1.load_samples
episode_folds = _r1.episode_folds
train_forward_reference = _r1.train_forward_reference
freeze_backend = _r1.freeze_backend
parameter_identity = _r1.parameter_identity
check_frozen = _r1.check_frozen
closed_prefix = _r1.closed_prefix
extract_feature = _r1.extract_feature
labels_for = _r1.labels_for
fit_head = _r1.fit_head
save_reload_head = _r1.save_reload_head
permuted_ids = _r1.permuted_ids
predict_head = _r1.predict_head
score_readout_row = _r1.score_readout_row
summarize_scores = _r1.summarize_scores
compare_generative_reference = _r1.compare_generative_reference
always_forward_reference = _r1.always_forward_reference


def prior_zero_spend(api):
    bindings = {
        name: api.explicit_binding(path, digest) for name, (path, digest) in FAILURE_PINS.items()
    }
    prior_plan, report, receipt = (
        api.read_bound(bindings[name]) for name in ("plan", "report", "receipt")
    )
    zero = dict.fromkeys(("attempted", "returned", "synchronized", "completed"), 0)
    counters = report.get("optimizer_steps")
    execution, observations, budget = (
        receipt.get(name, {}) for name in ("actual_execution", "observations", "cumulative_budget")
    )
    if (
        report.get("contract") != _r1.CONTRACT
        or type(report.get("native_exit_code")) is not int
        or report["native_exit_code"] != 1
        or report.get("success") is not False
        or report.get("model_loaded") is not True
        or report.get("head_locked_before_dev59") is not False
        or any(
            type(report.get(k)) is not int or report[k] != 0
            for k in ("feature_count", "raw_prediction_count")
        )
        or type(counters) is not dict
        or set(counters) != set(zero)
        or any(type(v) is not int or v != 0 for v in counters.values())
        or canonical(report.get("plan")) != canonical(bindings["plan"])
        or receipt.get("contract")
        != ("multidetect.onboard-navigation-action-readout-loader-failure-evidence-v1")
        or type(execution.get("actual_native_exit_code")) is not int
        or execution["actual_native_exit_code"] != 1
        or execution.get("worker_pid_absent") is not True
        or type(execution.get("relevant_model_processes_at_exit_check")) is not int
        or execution["relevant_model_processes_at_exit_check"] != 0
        or execution.get("output_files") != ["report.json"]
        or observations.get("success") is not False
        or observations.get("model_loaded") is not True
        or observations.get("complete_base_streaming_read_observed") is not False
        or any(
            type(observations.get(k)) is not int or observations[k] != 0
            for k in ("features", "raw_predictions")
        )
        or canonical(observations.get("optimizer_steps")) != canonical(zero)
        or budget.get("experiment_family") != "onboard-navigation-action-readout-20261008"
        or any(
            type(budget.get(k)) is not int or budget[k] != value
            for k, value in (
                ("maximum_total_head_optimizer_attempts", 800),
                ("this_failed_attempt_head_optimizer_attempts", 0),
                ("remaining_maximum_head_optimizer_attempts", 800),
                ("inherited_base_lora_optimizer_attempts", 280),
            )
        )
        or budget.get("inherited_base_lora_budget_reset") is not False
        or budget.get("new_attempt_may_not_grant_a_second_800_budget") is not True
        or prior_plan.get("rule") != RULE
    ):
        raise ValueError("Exactly bound closed r1 zero-spend failure and shared800 budget required")
    declared = {str(api.bound_path(row)): row for row in receipt["bindings"]}
    for row in receipt["bindings"]:
        api.verify(row)
    for row in (*bindings.values(), *(small_record(name) for name in R1_PINS)):
        if row == bindings["receipt"]:
            continue
        actual = declared.get(str(api.bound_path(row)))
        if actual is None or actual["bytes"] != row["bytes"] or actual["sha256"] != row["sha256"]:
            raise ValueError("Failure receipt must bind exact r1 source/test/plan/report")
    return {
        "bindings": bindings,
        "optimizer_steps": zero,
        "feature_count": 0,
        "raw_prediction_count": 0,
        "maximum_family_optimizer_attempts": 800,
        "spent_before": 0,
        "remaining_maximum_attempts": 800,
        "new_budget_granted": False,
        "receipt_bindings": receipt["bindings"],
    }, prior_plan


def build_plan(output_dir):
    plan = _r1.build_plan(output_dir)
    api = evaluation_api()
    prior, prior_plan = prior_zero_spend(api)
    for key in (
        "rule",
        "folds",
        "old86_ids",
        "dev59_ids",
        "dev_actor_identities",
        "train_derived_forward_reference_target",
        "prepared_inputs",
        "legacy_plan",
        "base_weights",
        "adapter_weights",
        "generative_reference86",
        "generative_reference59",
        "expected_base_parameter_sha256",
        "expected_adapter_parameter_sha256",
    ):
        if canonical(plan[key]) != canonical(prior_plan[key]):
            raise ValueError("v2 may change import order only, not the locked experiment")
    plan.update(
        contract=CONTRACT + "-plan",
        prior_failed_attempt=prior,
        import_order="versions_then_scope_hooks_then_actual_modules_then_backend_then_head",
        preimported_model_reader_policy="reject_fresh_process_required",
    )
    plan["source_files"] = api.normalize(
        [
            *plan["source_files"],
            *prior["bindings"].values(),
            *prior["receipt_bindings"],
            *(small_record(name) for name in NEW_FILES),
        ]
    )
    return plan


def admit_plan(path, digest):
    api = evaluation_api()
    binding = api.explicit_binding(safe_path(path), digest)
    plan = api.read_bound(binding)
    api.authenticate_code([r for r in plan["source_files"] if Path(r["path"]).suffix == ".py"])
    if canonical(plan) != canonical(build_plan(plan["output_dir"])):
        raise ValueError("v2 plan differs from the import-order-only shared-budget experiment")
    return plan, binding, api


def require_uncached_model_reader():
    if "transformers.modeling_utils" in sys.modules:
        raise ValueError("Preimported model reader denied; fresh process required")


def verify_streaming_alias():
    root = sys.modules.get("safetensors")
    modeling = sys.modules.get("transformers.modeling_utils")
    if (
        root is None
        or modeling is None
        or not callable(getattr(root, "safe_open", None))
        or getattr(modeling, "safe_open", None) is not root.safe_open
    ):
        raise ValueError("Model reader must capture the installed streaming safe_open alias")
    torch_module = sys.modules.get("safetensors.torch")
    if torch_module is not None and getattr(torch_module, "safe_open", None) is not root.safe_open:
        raise ValueError("Torch safe_open alias differs inside streaming scope")


def run(args):
    plan, plan_binding, api = admit_plan(args.plan, args.plan_sha256)
    before = api.capture_sources(plan, plan_binding)
    output = safe_path(plan["output_dir"], fresh=True)
    output.mkdir(exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "plan": plan_binding,
        "native_exit_code": 1,
        "success": False,
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "optimizer_steps": dict.fromkeys(("attempted", "returned", "synchronized", "completed"), 0),
        "source_bindings_before": before,
        "source_bindings_unchanged": False,
        "model_loaded": False,
        "head_locked_before_dev59": False,
        "feature_count": 0,
        "raw_prediction_count": 0,
        "rule": plan["rule"],
        "prior_failed_attempt": plan["prior_failed_attempt"],
        "combined_optimizer_steps": dict(plan["prior_failed_attempt"]["optimizer_steps"]),
    }
    backend = None
    try:
        report["runtime_admission"] = admit_runtime(plan, api)
        env = api.apis()
        env.eval = api
        closed = importlib.import_module("scripts.onboard_navigation_closed_sample_encoding")
        legacy = api.read_bound(plan["legacy_plan"])
        old = api.read_bound(legacy["original_plan"])
        samples = load_samples(old["dataset_samples"], api)
        if (
            episode_folds(samples) != plan["folds"]
            or [s.sample_id for s in samples] != plan["old86_ids"]
            or train_forward_reference(samples, api)
            != plan["train_derived_forward_reference_target"]
        ):
            raise ValueError("Original86 fold identities changed")
        prepared = api.read_bound(plan["prepared_inputs"])
        references = {
            r["sample_id"]: r for r in api.read_bound(legacy["cpu_prefix_report"])["samples"]
        }
        inherited = plan["evaluation_plan"]
        recovery = api.read_bound(inherited["recovery_plan"])
        budget = env.recovery.RecoveryModelBudget(**inherited["model_budget"])
        budget.validate()
        run_dir = Path(recovery["output_dir"])
        loader = {"phase": "reload", "pid": os.getpid()}
        report["loader_observation"] = loader
        report["model_loading_attempted"] = True
        require_uncached_model_reader()
        with env.old.loading_scope(legacy, old, run_dir / "reload", loader):
            verify_streaming_alias()
            runtime_modules = verify_runtime_modules(plan, api)
            verify_streaming_alias()
            report["runtime_modules_before"] = runtime_modules
            report["model_reader_imported_inside_streaming_scope"] = True
            backend = env.old.load_backend(
                legacy, budget, old, env.pilot, "reload", run_dir / "reload", loader
            )
        env.recovery.check_loader_cleanup(loader, legacy, old, run_dir)
        report["model_loaded"] = loader["model_loaded"] is True
        head_api = importlib.import_module("scripts.onboard_navigation_action_readout")
        initial = freeze_backend(backend, env)
        if initial != {
            "base": plan["expected_base_parameter_sha256"],
            "adapter": plan["expected_adapter_parameter_sha256"],
        }:
            raise ValueError("Exact actual frozen candidate parameters differ")
        report["model_parameter_identity_before"] = initial
        features, observations = {}, {}
        runtime_path = next(
            r["path"]
            for r in plan["runtime_sources"]
            if r["path"].replace("\\", "/").endswith("/qwen3_vl/modeling_qwen3_vl.py")
        )

        def extract(sample, *, expected=None, reference=None):
            started = time.perf_counter()
            prefix, identity = closed_prefix(
                sample, backend, env, closed, expected=expected, reference=reference
            )
            records = {k: env.pilot.tensor_record(v, backend.torch) for k, v in prefix.items()}
            if expected is not None and records != expected["prefix_tensors"]:
                raise ValueError("Closed old86 actual prefix tensor bytes differ")
            if expected is None and identity != plan["dev_actor_identities"][sample.sample_id]:
                raise ValueError("Original59 shared Actor identity differs")
            feature, elapsed = extract_feature(
                backend, prefix, head_api, expected_runtime_path=runtime_path
            )
            del prefix
            observations[sample.sample_id] = {
                "sample_id": sample.sample_id,
                "actor_identity_sha256": identity,
                "prefix_tensors": records,
                "feature": env.pilot.tensor_record(feature, backend.torch),
                "fused_forward_elapsed_s": elapsed,
                "source_encoding_feature_elapsed_s": time.perf_counter() - started,
                "source_session_closed_verified": True,
                "gold_in_model_inputs": False,
            }
            features[sample.sample_id] = feature
            report["feature_count"] += 1
            print(
                json.dumps(
                    {
                        "event": "readout_feature_admitted",
                        "sample_id": sample.sample_id,
                        "feature_count": report["feature_count"],
                    }
                ),
                flush=True,
            )

        for sample in samples:
            extract(
                sample, expected=prepared[sample.sample_id], reference=references[sample.sample_id]
            )
        head_records, raw_rows = {}, []
        by_id = {s.sample_id: s for s in samples}
        with (
            (output / "head-updates.jsonl").open("xb") as updates,
            (output / "predictions.jsonl").open("xb") as predictions,
        ):
            for number, fold in enumerate(
                plan["folds"] + [{"train_ids": plan["old86_ids"], "heldout_ids": []}]
            ):
                name = f"fold-{number}" if number < 3 else "final86"
                ids = fold["train_ids"]
                matrix = backend.torch.cat([features[sid] for sid in ids])
                head, identity = fit_head(
                    matrix,
                    labels_for([by_id[sid] for sid in ids], env, head_api),
                    head_api=head_api,
                    torch=backend.torch,
                    seed=RULE["seed"] + number,
                    updates=RULE["updates_per_head"],
                    stream=updates,
                    ledger=report["optimizer_steps"],
                    head_id=name,
                )
                locked, files = save_reload_head(
                    head,
                    output / name,
                    {
                        "contract": CONTRACT + "-head",
                        "rule": plan["rule"],
                        "head_id": name,
                        "train_ids": ids,
                        "feature_matrix": env.pilot.tensor_record(matrix, backend.torch),
                        **identity,
                    },
                    head_api,
                    backend.torch,
                    api,
                )
                with backend.torch.no_grad():
                    original_raw = head(matrix)
                    reloaded_raw = locked(matrix)
                if not backend.torch.equal(original_raw, reloaded_raw):
                    raise ValueError("Saved/reloaded head changed actual six-output tensor bytes")
                files["saved_reload_raw_outputs_exact"] = True
                files["raw_reload_comparison"] = env.pilot.tensor_record(
                    original_raw, backend.torch
                )
                del original_raw, reloaded_raw
                head_records[name] = {**identity, **files}
                bindings = {**plan, **files}
                held = fold["heldout_ids"]
                for condition, sources in (
                    ("loeo", held),
                    (
                        "same_episode_permutation",
                        permuted_ids(held, RULE["seed"] + number) if held else [],
                    ),
                ):
                    for sid, feature_sid in zip(held, sources, strict=True):
                        row = predict_head(
                            locked,
                            sid,
                            features[feature_sid],
                            observations[feature_sid],
                            bindings,
                            head_api,
                        )
                        row.update(condition=condition, head_id=name)
                        row.update(partition=by_id[sid].partition, episode_id=by_id[sid].episode_id)
                        api.append_row(predictions, row)
                        raw_rows.append(row)
                        report["raw_prediction_count"] += 1
                if number == 3:
                    final_head, final_bindings = locked, bindings
                del head, matrix
            report["head_locked_before_dev59"] = True
            report["heads"] = head_records
            check_frozen(backend)
            if parameter_identity(backend, env) != initial:
                raise ValueError("Base/LoRA changed during feature/head stage")
            _all, dev, _actors = api.admit_dataset(api.read_bound(inherited["dataset_report"]), env)
            if [s.sample_id for s in dev] != plan["dev59_ids"]:
                raise ValueError("Only locked17+42 dev rows may enter model")
            for sample in dev:
                extract(sample)
                row = predict_head(
                    final_head,
                    sample.sample_id,
                    features[sample.sample_id],
                    observations[sample.sample_id],
                    final_bindings,
                    head_api,
                )
                row.update(condition="locked_final86_dev59", head_id="final86")
                row.update(partition=sample.partition, episode_id=sample.episode_id)
                api.append_row(predictions, row)
                raw_rows.append(row)
                report["raw_prediction_count"] += 1
            # All raw outputs are fsynced and the prediction file closed before gold scoring.
        all_samples = by_id | {s.sample_id: s for s in dev}
        scores = []
        for row in raw_rows:
            sample = all_samples[row["sample_id"]]
            score = score_readout_row(row, sample, api, env)
            scores.append(
                {
                    "sample_id": row["sample_id"],
                    "condition": row["condition"],
                    "partition": row["partition"],
                    "score": score,
                }
            )
        report["score_counts"] = {
            condition: summarize_scores([r for r in scores if r["condition"] == condition])
            for condition in ("loeo", "same_episode_permutation", "locked_final86_dev59")
        }
        report["generative_reference_comparison"] = compare_generative_reference(
            plan, scores, all_samples, api, env, observations
        )
        report["mathematical_always_forward_dev59"] = always_forward_reference(
            [r for r in scores if r["condition"] == "locked_final86_dev59"],
            plan["train_derived_forward_reference_target"],
            all_samples,
            api,
            env,
        )
        report["dev_partition_scores"] = {
            part: summarize_scores(
                [
                    r
                    for r in scores
                    if r["condition"] == "locked_final86_dev59" and r["partition"] == part
                ]
            )
            for part in ("dev-validation", "dev-test")
        }
        if (
            report["feature_count"] != 145
            or report["raw_prediction_count"] != 231
            or report["optimizer_steps"] != dict.fromkeys(report["optimizer_steps"], 800)
        ):
            raise ValueError(
                "Exact145 features/231 predictions/800 completed head updates required"
            )
        api.create_json(output / "scores.json", scores)
        api.create_json(output / "features.json", list(observations.values()))
        after_identity = parameter_identity(backend, env)
        report["model_parameter_identity_after"] = after_identity
        check_frozen(backend)
        if after_identity != initial:
            raise ValueError("Actual base/LoRA parameter bytes changed")
        for row in head_records.values():
            api.verify(row["weights"])
            api.verify(row["config"])
        report["runtime_modules_after"] = verify_runtime_modules(plan, api)
        if canonical(report["runtime_modules_after"]) != canonical(runtime_modules):
            raise ValueError("Actual imported runtime identity changed before seal")
        after = api.capture_sources(plan, plan_binding)
        report["source_bindings_after"] = after
        if canonical(after) != canonical(before):
            raise ValueError("Source/runtime inventory changed before seal")
        report.update(
            native_exit_code=0,
            success=True,
            source_bindings_unchanged=True,
            predictions=api.record(output / "predictions.jsonl"),
            scores=api.record(output / "scores.json"),
            features=api.record(output / "features.json"),
            head_updates=api.record(output / "head-updates.jsonl"),
            raw_durable_before_gold_scoring=True,
            unsupported_dev_rows_retained=2,
        )
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        traceback.print_exc()
    finally:
        report["combined_optimizer_steps"] = {
            key: plan["prior_failed_attempt"]["optimizer_steps"][key] + value
            for key, value in report["optimizer_steps"].items()
        }
        if backend is not None or report.get("loader_observation", {}).get("model_loaded") is True:
            report["model_loaded"] = True
        backend = None
        gc.collect()
        api.create_json(output / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-plan", action="store_true")
    mode.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    parser.add_argument("--output-dir")
    parser.add_argument("--plan-output")
    args = parser.parse_args(argv)
    try:
        if args.emit_plan:
            if not args.output_dir or args.plan_sha256:
                raise ValueError("Plan emission requires only a fresh output directory")
            plan = build_plan(args.output_dir)
            if args.plan_output:
                path = safe_path(args.plan_output, fresh=True)
                if (
                    path.parent != ROOT / "configs/training"
                    or path.suffix != ".json"
                    or not path.name.startswith("onboard-navigation-action-readout-")
                ):
                    raise ValueError("Only a NEW configs/training action-readout plan is allowed")
                path.parent.mkdir(parents=True, exist_ok=True)
                safe_path(path, fresh=True)
                api = evaluation_api()
                api.create_json(path, plan)
                print(json.dumps(api.record(path), ensure_ascii=False, sort_keys=True))
                return 0
            print(
                json.dumps(
                    plan,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
            )
            return 0
        if not args.plan_sha256 or args.output_dir or args.plan_output:
            raise ValueError("Execute requires only explicit plan and SHA")
        return run(args)["native_exit_code"]
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

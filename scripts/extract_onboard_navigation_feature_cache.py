"""Create-only actual candidate267 feature cache for old86 + original TRAIN122.

Only execute loads weights. No head, optimizer, generation, or flight is run.
The shared closed encoder may construct a host-only supervision suffix; only
the exact four independently admitted prefix tensors reach Qwen3VLModel.
Inherited explicit inventories are retained, not a recursive repository scan.
The resulting component receipt does not replace this worker's closed report.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib
import inspect
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "multidetect.onboard-navigation-candidate208-feature-cache-v1"
NEW_FILES = (
    "scripts/extract_onboard_navigation_feature_cache.py",
    "tests/test_extract_onboard_navigation_feature_cache.py",
)
PINS = {
    "scripts/experiment_onboard_navigation_action_readout_v2.py": (
        25831,
        "cbcba9425112a87cf0b1c867219777c7a6c593344cca0cddc7039954bdae5f44",
    ),
    "scripts/verify_onboard_navigation_indexed_cpu_inputs_v2.py": (
        23702,
        "d65757e1dbd51a4301a49b3977ca12189f2ecf1e734ad177563d9c56fd554325",
    ),
    "scripts/onboard_navigation_stable_readout.py": (
        31669,
        "b756841607993be3df6e8d818e1c0398a70105a7e19c801d33e034df000cbbc2",
    ),
    "tests/test_onboard_navigation_stable_readout.py": (
        23622,
        "87ad8a630882f57f60171bf004483e41bd5e59eee5da7dce3cf9ad65f4a052c9",
    ),
}
BOUND = {
    "readout_plan": (
        "configs/training/onboard-navigation-action-readout-20261008-r2.json",
        "9e08595170078130e900c4012316970452a93b3ed46042f2b6d535203230414c",
    ),
    "readout_report": (
        "artifacts/evaluation/onboard-navigation-action-readout-20261008-r2/report.json",
        "a4ebd36b0c4ef7cb3fc2bcfea6fa6358ee7567705eb39b6cd177fd9740ed1765",
    ),
    "cpu122_receipt": (
        "docs/evidence/onboard-navigation-indexed-cpu122-admission-20261008-r3.json",
        "d11cfad20fb9b5f40a1f20a3aa37a9b78b77ba64e164fdf8f51ad3ad87111ca3",
    ),
}
RULE = {
    "feature_source": "actual_exact_recovery267_candidate_only",
    "feature_dim": 2048,
    "sample_count": 208,
    "old86_count": 86,
    "new122_count": 122,
    "maximum_fused_prefill_attempts": 208,
    "maximum_generation_calls": 0,
    "maximum_optimizer_attempts": 0,
    "maximum_retained_feature_bytes": 1703936,
    "prefix_input_tokens": 4096,
    "max_pixels_per_image": 50176,
    "supervision_response_tokens": 192,
    "cpu_threads": 1,
    "model_forward": "Qwen3VLModel.last_hidden_state_last_nonzero_prefix_mask",
    "input_scope": "verbatim_prompt_current_and_past_no_gold_pose_future",
    "input_residency": "one_prefix_sample_plus_small_detached_CPU_feature_rows",
}
FALSE_CLAIMS = (
    "head_created",
    "optimizer_performed",
    "training_performed",
    "text_generation_performed",
    "pristine_base_or_warm_comparison",
    "quality_claim",
    "generalization_verified",
    "official_holdout_verified",
    "candidate_promoted",
    "default_model_changed",
    "execution_authority",
    "flight_performed",
    "physical_performance_claimed",
    "hil_real_evidence",
    "outdoor_gnss_evidence",
    "final_release_allowed",
    "end_to_end_realtime_latency_verified",
    "training_budget_reset",
    "new_optimizer_budget_granted",
)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def bootstrap():
    # Authenticate every directly imported repository module BEFORE executing it.
    for name, (size, digest) in PINS.items():
        path = ROOT / name
        if path.stat().st_size != size or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Frozen cache dependency drift: {name}")
    previous = sys.path[:]
    try:
        sys.path[:0] = [str(ROOT), str(ROOT / "src")]
        readout = importlib.import_module("scripts.experiment_onboard_navigation_action_readout_v2")
        cpu = importlib.import_module("scripts.verify_onboard_navigation_indexed_cpu_inputs_v2")
        stable = importlib.import_module("scripts.onboard_navigation_stable_readout")
        for module, name in (
            (readout, "scripts/experiment_onboard_navigation_action_readout_v2.py"),
            (cpu, "scripts/verify_onboard_navigation_indexed_cpu_inputs_v2.py"),
            (stable, "scripts/onboard_navigation_stable_readout.py"),
        ):
            if Path(module.__file__).resolve() != (ROOT / name).resolve():
                raise ValueError("Actual repository module origin differs")
        api = readout.evaluation_api()
        cpu.base.bootstrap()
        return SimpleNamespace(readout=readout, cpu=cpu, stable=stable, api=api)
    finally:
        sys.path[:] = previous


def fresh_output(value, readout):
    path = readout.safe_path(value, fresh=True)
    if path.parent != ROOT / "artifacts/training" or not path.name.startswith(
        "onboard-navigation-candidate208-features-"
    ):
        raise ValueError("One fresh direct candidate208 training-artifact directory required")
    return path


def cpu122_inputs(receipt, api, cpu):
    execution, observed = receipt["actual_execution"], receipt["observations"]
    if (
        receipt["contract"] != "multidetect.onboard-navigation-indexed-cpu122-admission-evidence-v2"
        or type(execution["actual_native_exit_code"]) is not int
        or execution["actual_native_exit_code"] != 0
        or execution["worker_pid_absent"] is not True
        or observed["success"] is not True
        or observed["training_dataset_cpu_admitted"] is not True
        or type(observed["sample_count"]) is not int
        or observed["sample_count"] != 122
        or observed["source_bindings_unchanged"] is not True
        or receipt["actual_execution_third_CPU122_run_claimed"] is not False
        or any(value is not False for value in receipt["claims"].values())
    ):
        raise ValueError("Root-sealed actual successful CPU122 r3 receipt required")
    declared = api.normalize(receipt["bindings"])
    # Distinguish the actual successful r2 report/plan from inherited r1 failures.
    actual_root = ROOT / "artifacts/diagnostics/onboard-navigation-indexed-cpu122-20261008-r2"

    def exact_binding(path):
        selected = [row for row in declared if Path(row["path"]).resolve() == path.resolve()]
        if len(selected) != 1:
            raise ValueError(f"One exact successful CPU122 binding required: {path}")
        return selected[0]

    report_row = exact_binding(actual_root / "report.json")
    report = api.read_bound(report_row)
    plan_row = exact_binding(
        ROOT / "configs/training/onboard-navigation-indexed-cpu122-20261008-r2.json"
    )
    plan = api.read_bound(plan_row)
    prepared_row = exact_binding(actual_root / "prepared-inputs.json")
    prefix_row = exact_binding(actual_root / "prefix-reference.json")
    prepared, prefix = api.read_bound(prepared_row), api.read_bound(prefix_row)
    if (
        report["contract"] != cpu.CONTRACT
        or report["native_exit_code"] != 0
        or type(report["native_exit_code"]) is not int
        or report["success"] is not True
        or report["training_dataset_cpu_admitted"] is not True
        or report["sample_count"] != 122
        or type(report["sample_count"]) is not int
        or report["source_validation_sessions_verified"] != 122
        or report["partial_failures"]
        or report["saved_cpu122_metadata_exact"] is not True
        or report["source_bindings_unchanged"] is not True
        or canonical(report["source_bindings_before"]) != canonical(report["source_bindings_after"])
        or canonical(report["source_bindings_before"]) != canonical(plan["sources"])
        or report["plan"]["sha256"] != plan_row["sha256"]
        or report["prepared_inputs"]["sha256"] != prepared_row["sha256"]
        or report["prefix_reference"]["sha256"] != prefix_row["sha256"]
        or report["admitted_sample_ids"] != plan["sample_ids"]
        or report["limits"] != cpu.base.LIMITS
        or report["optional_arrow_isolation"]["own_finder_removed"] is not True
        or report["optional_arrow_isolation"]["prior_finders_preserved"] is not True
        or any(report[key] is not False for key in cpu.base.FALSE_CLAIMS)
    ):
        raise ValueError("Actual CPU122 successful report, source closure, or metadata differs")
    return (
        plan,
        prepared,
        prefix["samples"],
        declared,
        {"plan": plan_row, "report": report_row, "prepared": prepared_row, "prefix": prefix_row},
    )


def sample_inputs(plan, api, env, cpu):
    cpu_plan = plan["cpu122_plan"]
    old_samples = env.navigation.NavigationSupervisionSample
    legacy = api.read_bound(plan["legacy_plan"])
    old = api.read_bound(legacy["original_plan"])
    samples86 = tuple(
        old_samples.from_json(line)
        for line in api.verify(old["dataset_samples"]).read_bytes().splitlines()
        if line.strip()
    )
    env.navigation.validate_navigation_episode_samples(samples86)
    source_report = api.read_bound(cpu_plan["source_report"])
    dependencies = SimpleNamespace(
        nav=env.navigation,
        actor=env.actor,
        closed=env.closed,
        encoding=env.encoding,
        pilot=env.pilot,
        checker=env.prefix,
    )
    rows = [
        api.strict_json(line)
        for line in api.verify(cpu_plan["dataset"]).read_bytes().splitlines()
        if line.strip()
    ]
    samples122 = cpu.base.admit_rows(rows, source_report, dependencies)
    prepared122 = api.read_bound(plan["cpu122_bindings"]["prepared"])
    references122 = api.read_bound(plan["cpu122_bindings"]["prefix"])["samples"]
    cpu.base.validate_table(references122, prepared122, samples122, source_report, dependencies)
    if [sample.sample_id for sample in samples86] != plan["old86_ids"]:
        raise ValueError("Original86 ordered metadata differs")
    if [sample.sample_id for sample in samples122] != cpu_plan["sample_ids"]:
        raise ValueError("Original TRAIN122 ordered metadata differs")
    samples = samples86 + samples122
    ids = [sample.sample_id for sample in samples]
    if (
        len(ids) != 208
        or len(set(ids)) != 208
        or any(
            sample.partition != "train" or sample.source.original_split != "train"
            for sample in samples
        )
        or len({sample.episode_id for sample in samples}) != 9
        or {sample.scene_id for sample in samples} != {"env_airsim_18", "env_airsim_26"}
        or set(prepared122) != set(cpu_plan["sample_ids"])
    ):
        raise ValueError("Exactly208 unique TRAIN rows from nine whole episodes required")
    return samples, legacy, old, prepared122, references122


def environment(api):
    env = api.apis()
    env.closed = importlib.import_module("scripts.onboard_navigation_closed_sample_encoding")
    env.encoding = importlib.import_module("scripts.onboard_navigation_balanced_encoding")
    return env


def build_plan(output_dir):
    controls = bootstrap()
    readout, cpu, api = controls.readout, controls.cpu, controls.api
    output = fresh_output(output_dir, readout)
    refs = {name: api.explicit_binding(path, digest) for name, (path, digest) in BOUND.items()}
    inherited = api.read_bound(refs["readout_plan"])
    prior = api.read_bound(refs["readout_report"])
    if (
        prior["contract"] != readout.CONTRACT
        or prior["native_exit_code"] != 0
        or type(prior["native_exit_code"]) is not int
        or prior["success"] is not True
        or prior["feature_count"] != 145
        or prior["raw_prediction_count"] != 231
        or any(
            type(prior["combined_optimizer_steps"][s]) is not int
            or prior["combined_optimizer_steps"][s] != 800
            for s in api.STAGES
        )
        or prior["source_bindings_unchanged"] is not True
        or canonical(prior["model_parameter_identity_before"])
        != canonical(prior["model_parameter_identity_after"])
        or any(prior["claims"][key] is not False for key in readout.FALSE_CLAIMS)
    ):
        raise ValueError("Closed actual r2 readout with inherited800 spend required")
    api.authenticate_code(
        [row for row in inherited["source_files"] if Path(row["path"]).suffix == ".py"]
    )
    env = environment(api)
    recovery = api.read_bound(inherited["evaluation_plan"]["recovery_plan"])
    terminal, train, _reload = api.verify_terminal(
        api.read_bound(inherited["references"]["terminal"]), recovery, env
    )
    cpu_plan, prepared, references, declared, cpu_bindings = cpu122_inputs(
        api.read_bound(refs["cpu122_receipt"]), api, cpu
    )
    api.authenticate_code(
        [row for row in api.normalize(cpu_plan["sources"]) if Path(row["path"]).suffix == ".py"]
    )
    projection = {**inherited, "cpu122_plan": cpu_plan, "cpu122_bindings": cpu_bindings}
    samples, _legacy, _old, prepared_again, references_again = sample_inputs(
        projection, api, env, cpu
    )
    if canonical(prepared_again) != canonical(prepared) or canonical(references_again) != canonical(
        references
    ):
        raise ValueError("CPU122 complete reference projection differs")
    image_rows = [
        {"path": frame.path, "bytes": frame.bytes, "sha256": frame.sha256}
        for sample in samples
        for frame in sample.frames
    ]
    source_files = api.normalize(
        [
            *inherited["source_files"],
            *cpu_plan["sources"],
            *cpu_plan["images"],
            *cpu_plan["processor_files"],
            *declared,
            *terminal.values(),
            *refs.values(),
            *(readout.small_record(name) for name in (*NEW_FILES, *PINS)),
            *image_rows,
        ]
    )
    runtime_sources = api.normalize(
        [
            *inherited["runtime_sources"],
            *cpu_plan["runtime"]["source_files"],
            *cpu_plan["optional_import_runtime"]["source_files"],
        ],
        runtime=True,
    )
    if prior["model_parameter_identity_after"] != {
        "base": inherited["expected_base_parameter_sha256"],
        "adapter": train["adapter_parameter_sha256_after"],
    }:
        raise ValueError("Frozen candidate identity differs from actual completed readout")
    return {
        "schema_version": 1,
        "contract": CONTRACT + "-plan",
        "output_dir": str(output),
        "rule": dict(RULE),
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "references": refs,
        "readout_plan": inherited,
        "cpu122_plan": cpu_plan,
        "cpu122_bindings": cpu_bindings,
        "legacy_plan": inherited["legacy_plan"],
        "old86_ids": inherited["old86_ids"],
        "sample_ids": [sample.sample_id for sample in samples],
        "episode_ids": [sample.episode_id for sample in samples],
        "scene_ids": [sample.scene_id for sample in samples],
        "partitions": [sample.partition for sample in samples],
        "source_files": source_files,
        "runtime_sources": runtime_sources,
        "terminal_bindings": terminal,
        "dataset_bindings": {"old86": _old["dataset_samples"], "new122": cpu_plan["dataset"]},
        "model_file_bindings": {"base": inherited["base_weights"], "adapter": terminal["weights"]},
        "expected_model_identity": prior["model_parameter_identity_after"],
        "inherited_head_optimizer_attempts": 800,
        "inherited_base_lora_optimizer_attempts": 280,
        "new_optimizer_attempts": 0,
        "source_inventory_scope": "explicit_inherited_admission_and_every_208_current_past_PNG",
    }


def admit_plan(path, digest):
    controls = bootstrap()
    api = controls.api
    binding = api.explicit_binding(controls.readout.safe_path(path), digest)
    plan = api.read_bound(binding)
    if canonical(plan) != canonical(build_plan(plan["output_dir"])):
        raise ValueError("Explicit208 cache plan differs before any model import")
    return controls, plan, binding


def actual_runtime_origins(plan, backend, api):
    # CPU122's validator has a CPU-only CUDA-state gate; authenticate the same
    # bound entrypoints/classes here without incorrectly applying that gate.
    allowed = {str(api.verify(row, runtime=True).resolve()) for row in plan["runtime_sources"]}
    names = (
        "torch",
        "torch._C",
        "transformers",
        "PIL",
        "PIL.Image",
        "PIL.ImageFile",
        "PIL.PngImagePlugin",
        "PIL._imaging",
        "numpy",
        "tokenizers",
        "tokenizers.tokenizers",
        "torchvision",
        "peft",
        "safetensors.torch",
    )
    observed = {}
    for name in names:
        path = Path(importlib.import_module(name).__file__).resolve()
        if str(path) not in allowed:
            raise ValueError(f"Actual imported runtime origin is unbound: {name}")
        observed[name] = str(path)
    for name, value in (
        ("processor", backend.processor),
        ("image_processor", backend.processor.image_processor),
        ("tokenizer", backend.processor.tokenizer),
    ):
        path = inspect.getsourcefile(type(value))
        if path is None or str(Path(path).resolve()) not in allowed:
            raise ValueError("Actual processor class origin is unbound")
        observed[name] = str(Path(path).resolve())
    if type(backend.processor).__name__ != "Qwen3VLProcessor":
        raise ValueError("Exact official Qwen3VLProcessor required")
    return observed


def closed_prefix(sample, backend, env, readout, *, expected, reference):
    sessions = []

    def factory(root):
        session = env.closed.ClosedNavigationSourceValidationSession(root)
        sessions.append(session)
        return session

    def encode(*args, **kwargs):
        return env.closed.encode_closed_navigation_sample(*args, **kwargs, session_factory=factory)

    prefix = None
    try:
        prefix, identity = readout.closed_prefix(
            sample,
            backend,
            env,
            SimpleNamespace(encode_closed_navigation_sample=encode),
            expected=expected,
            reference=reference,
        )
        if len(sessions) != 1 or sessions[0].verified is not True or sessions[0]._active:
            raise ValueError("Exactly one normally closed verified source session required")
        if set(prefix) != {"input_ids", "attention_mask", "pixel_values", "image_grid_thw"}:
            raise ValueError("Only four prefix tensors may enter the feature model")
        records = {
            name: env.pilot.tensor_record(value, backend.torch) for name, value in prefix.items()
        }
        if (
            canonical(records) != canonical(expected["prefix_tensors"])
            or identity != expected["actor_identity_sha256"]
            or identity != reference["actor_identity_sha256"]
        ):
            raise ValueError("CPU-admitted Actor/four-prefix byte identity differs")
        return prefix, identity, records
    except BaseException:
        prefix = None
        raise


def extract_rows(samples, backend, env, controls, plan, report, prepared, references):
    features, observations = [], []
    runtime_path = next(
        row["path"]
        for row in plan["runtime_sources"]
        if row["path"].replace("\\", "/").endswith("/qwen3_vl/modeling_qwen3_vl.py")
    )
    for sample in samples:
        prefix, feature = None, None
        started = time.perf_counter()
        try:
            prefix, actor, records = closed_prefix(
                sample,
                backend,
                env,
                controls.readout,
                expected=prepared[sample.sample_id],
                reference=references[sample.sample_id],
            )
            if report["fused_prefill_attempts"] >= RULE["maximum_fused_prefill_attempts"]:
                raise ValueError("Precommitted208 forward budget exhausted")
            report["fused_prefill_attempts"] += 1
            feature, elapsed = controls.readout.extract_feature(
                backend, prefix, env.head_api, expected_runtime_path=runtime_path
            )
            del prefix
            prefix = None
            feature = controls.stable._tensor(feature, shape=(1, RULE["feature_dim"]))
            if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError("Finite nonnegative actual fused-forward duration required")
            observations.append(
                {
                    "sample_id": sample.sample_id,
                    "actor_identity_sha256": actor,
                    "prefix_tensors": records,
                    "feature": env.pilot.tensor_record(feature, backend.torch),
                    "fused_forward_elapsed_s": elapsed,
                    "source_encoding_feature_elapsed_s": time.perf_counter() - started,
                    "source_session_closed_verified": True,
                    "gold_in_model_inputs": False,
                }
            )
            features.append(feature.detach().clone())
            report["feature_count"] = len(features)
            report["source_validation_sessions_verified"] = len(features)
            print(
                json.dumps(
                    {
                        "event": "candidate208_feature_admitted",
                        "sample_id": sample.sample_id,
                        "feature_count": len(features),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        finally:
            del prefix, feature
    if len(features) != 208 or [row["sample_id"] for row in observations] != plan["sample_ids"]:
        raise ValueError("Complete208 ordered actual features required")
    matrix = backend.torch.cat(features, dim=0)
    del features
    controls.stable._tensor(matrix, shape=(208, 2048))
    return matrix, observations


def run(plan_path, digest):
    controls, plan, plan_binding = admit_plan(plan_path, digest)
    api, readout, cpu = controls.api, controls.readout, controls.cpu
    before = api.capture_sources(plan, plan_binding)
    output = fresh_output(plan["output_dir"], readout)
    output.mkdir(exist_ok=False)
    report = {
        "schema_version": 1,
        "contract": CONTRACT,
        "plan": plan_binding,
        "pid": os.getpid(),
        "success": False,
        "native_exit_code": 1,
        "feature_cache_admitted": False,
        "feature_count": 0,
        "fused_prefill_attempts": 0,
        "source_validation_sessions_verified": 0,
        "source_sessions_verified": False,
        "all208_prefix_metadata_equal": False,
        "cache_saved_and_exactly_reloaded": False,
        "model_loaded": False,
        "claims": dict.fromkeys(FALSE_CLAIMS, False),
        "source_bindings_before": before,
        "source_bindings_unchanged": False,
        "optimizer_steps": dict.fromkeys(api.STAGES, 0),
        "inherited_head_optimizer_attempts": 800,
        "inherited_base_lora_optimizer_attempts": 280,
        "new_optimizer_attempts": 0,
        "generation_calls": 0,
        "gold_full_cpu_suffix_constructed_not_forwarded": True,
        "model_inputs": ["input_ids", "attention_mask", "pixel_values", "image_grid_thw"],
        "partial_failures": [],
        "rule": dict(RULE),
        "sample_ids": plan["sample_ids"],
        "episode_ids": plan["episode_ids"],
        "scene_ids": plan["scene_ids"],
        "partitions": plan["partitions"],
        "dataset_bindings": plan["dataset_bindings"],
        "model_file_bindings": plan["model_file_bindings"],
        "optional_arrow_isolation": {"policy": cpu.ISOLATION_POLICY},
    }
    backend, matrix = None, None
    try:
        report["runtime_admission"] = readout.admit_runtime(plan["readout_plan"], api)
        with cpu.OptionalArrowIsolation(report["optional_arrow_isolation"]):
            env = environment(api)
            samples, legacy, old, prepared122, references122 = sample_inputs(plan, api, env, cpu)
            recovery = api.read_bound(plan["readout_plan"]["evaluation_plan"]["recovery_plan"])
            budget = env.recovery.RecoveryModelBudget(
                **plan["readout_plan"]["evaluation_plan"]["model_budget"]
            )
            budget.validate()
            run_dir = Path(recovery["output_dir"])
            loader = {"phase": "reload", "pid": os.getpid()}
            report["loader_observation"] = loader
            readout.require_uncached_model_reader()
            with env.old.loading_scope(legacy, old, run_dir / "reload", loader):
                readout.verify_streaming_alias()
                report["runtime_modules_before"] = readout.verify_runtime_modules(
                    plan["readout_plan"], api
                )
                readout.verify_streaming_alias()
                backend = env.old.load_backend(
                    legacy, budget, old, env.pilot, "reload", run_dir / "reload", loader
                )
            env.recovery.check_loader_cleanup(loader, legacy, old, run_dir)
            report["model_loaded"] = loader["model_loaded"] is True
            backend.torch.set_num_threads(RULE["cpu_threads"])
            if backend.torch.get_num_threads() != RULE["cpu_threads"]:
                raise ValueError("Explicit one-thread host encoding policy differs")
            env.head_api = importlib.import_module("scripts.onboard_navigation_action_readout")
            initial = readout.freeze_backend(backend, env)
            report["model_parameter_identity_before"] = initial
            if canonical(initial) != canonical(plan["expected_model_identity"]):
                raise ValueError("Actual loaded candidate267 parameters differ")
            origins = actual_runtime_origins(plan, backend, api)
            report["actual_runtime_origins_before"] = origins
            optional_origins = cpu.validate_optional_origins(
                plan["cpu122_plan"]["optional_import_runtime"]
            )
            report["optional_python_origins_before"] = optional_origins
            prepared86 = api.read_bound(plan["readout_plan"]["prepared_inputs"])
            references86 = api.read_bound(legacy["cpu_prefix_report"])["samples"]
            prepared = {**prepared86, **prepared122}
            references = {row["sample_id"]: row for row in (*references86, *references122)}
            if set(prepared) != set(plan["sample_ids"]) or set(references) != set(
                plan["sample_ids"]
            ):
                raise ValueError("Exactly208 admitted CPU references required")
            matrix, observations = extract_rows(
                samples, backend, env, controls, plan, report, prepared, references
            )
            report["source_sessions_verified"] = True
            report["all208_prefix_metadata_equal"] = True
            readout.check_frozen(backend)
            final = readout.parameter_identity(backend, env)
            report["model_parameter_identity_after"] = final
            if canonical(final) != canonical(initial):
                raise ValueError("Actual base/adapter parameter bytes changed during extraction")
            report["actual_runtime_origins_after"] = actual_runtime_origins(plan, backend, api)
            if canonical(report["actual_runtime_origins_after"]) != canonical(origins):
                raise ValueError("Actual imported runtime origin drift before seal")
            report["optional_python_origins_after"] = cpu.validate_optional_origins(
                plan["cpu122_plan"]["optional_import_runtime"]
            )
            if canonical(report["optional_python_origins_after"]) != canonical(optional_origins):
                raise ValueError("Actual optional Python origin drift before seal")
            report["runtime_modules_after"] = readout.verify_runtime_modules(
                plan["readout_plan"], api
            )
            if canonical(report["runtime_modules_before"]) != canonical(
                report["runtime_modules_after"]
            ):
                raise ValueError("Actual model-reader runtime module drift")
            after = api.capture_sources(plan, plan_binding)
            report["source_bindings_after"] = after
            if canonical(before) != canonical(after):
                raise ValueError("Explicit source inventory changed before cache publication")
            report["source_bindings_unchanged"] = True
            cpu.base.assert_no_forbidden_modules()
            # Small receipt/plan/report inputs bind the full worker evidence.
            cache_sources = api.normalize(
                [
                    plan_binding,
                    *plan["references"].values(),
                    *plan["cpu122_bindings"].values(),
                ]
            )
            sources = [{**row, "path": api.bound_path(row).as_posix()} for row in cache_sources]
            receipt = controls.stable.save_feature_cache(
                output / "cache",
                matrix,
                observations=observations,
                model_identity=initial,
                partitions=plan["partitions"],
                episode_ids=plan["episode_ids"],
                extraction_metadata={
                    "worker_plan": plan_binding,
                    "candidate267_only": True,
                    "runtime": report["runtime_admission"],
                    "cpu122_receipt": plan["references"]["cpu122_receipt"],
                    "input_scope": RULE["input_scope"],
                    "inherited_optimizer_spend": {"head": 800, "base_lora": 280},
                },
                sources=sources,
            )
            clone, metadata = controls.stable.load_feature_cache(
                output / "cache",
                receipt_sha256=receipt["sha256"],
                expected_sample_ids=plan["sample_ids"],
                expected_model_identity=initial,
            )
            if not backend.torch.equal(clone, matrix):
                raise ValueError("Actual feature matrix exact reload differs")
            report["cache_saved_and_exactly_reloaded"] = True
            del clone, metadata
            report["cache_receipt"] = receipt
            report["feature_matrix"] = env.pilot.tensor_record(matrix, backend.torch)
            report["cache_tensor_bytes"] = matrix.numel() * matrix.element_size()
            api.create_json(
                output / "manifest.json",
                {
                    "contract": CONTRACT + "-manifest",
                    "plan": plan_binding,
                    "cache_receipt": receipt,
                    "model_identity": initial,
                    "sample_ids": plan["sample_ids"],
                    "episode_ids": plan["episode_ids"],
                    "partitions": plan["partitions"],
                    "feature_matrix": report["feature_matrix"],
                    "claims": report["claims"],
                },
            )
            report["manifest"] = api.record(output / "manifest.json")
            report["source_bindings_unchanged"] = False
            report["source_bindings_after"] = api.capture_sources(plan, plan_binding)
            if canonical(report["source_bindings_after"]) != canonical(before):
                raise ValueError("Source drift after cache publication; bundle is not admitted")
            report["source_bindings_unchanged"] = True
        # Optional-finder cleanup is part of admission, never provisional success.
        cpu.base.assert_no_forbidden_modules()
        report.update(success=True, native_exit_code=0, feature_cache_admitted=True)
    except BaseException as error:
        report["partial_failures"].append(
            {
                "type": type(error).__name__,
                "message": str(error),
                "completed_features": report["feature_count"],
                "fused_prefill_attempts": report["fused_prefill_attempts"],
            }
        )
        traceback.print_exc()
    finally:
        if backend is not None or report.get("loader_observation", {}).get("model_loaded") is True:
            report["model_loaded"] = True
        backend, matrix = None, None
        gc.collect()
        api.create_json(output / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-plan")
    parser.add_argument("--output-dir")
    parser.add_argument("--plan")
    parser.add_argument("--plan-sha256")
    args = parser.parse_args(argv)
    try:
        if args.emit_plan and args.output_dir and not args.plan and not args.plan_sha256:
            controls = bootstrap()
            target = controls.readout.safe_path(args.emit_plan, fresh=True)
            if (
                target.parent != ROOT / "configs/training"
                or target.suffix != ".json"
                or not target.name.startswith("onboard-navigation-candidate208-features-")
            ):
                raise ValueError("New feature-cache plan must stay in configs/training")
            controls.api.create_json(target, build_plan(args.output_dir))
            print(json.dumps(controls.api.record(target), sort_keys=True))
            return 0
        if args.plan and args.plan_sha256 and not args.emit_plan and not args.output_dir:
            report = run(args.plan, args.plan_sha256)
            print(
                json.dumps(
                    {
                        "event": "candidate208_feature_cache_finished",
                        "success": report["success"],
                        "native_exit_code": report["native_exit_code"],
                        "feature_count": report["feature_count"],
                    },
                    sort_keys=True,
                )
            )
            return report["native_exit_code"]
        raise ValueError("Emit requires NEW plan/output; execute requires explicit plan SHA only")
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
